"""AI lead analysis (Phase 7) - eligible leads only, router as the single AI boundary.

``LeadAnalysisService.run(...)`` per company:
  1. ELIGIBILITY  re-checked at this boundary: ``ensure_enrichment_allowed`` (latest decision
                  QUALIFIED, current qualification policy + ICP config) AND a VERIFIED Phase 6
                  contact enriched under that same decision. Otherwise a BLOCKED record and no
                  model call.
  2. BUNDLE       compact evidence (clustered events, own pages, qualification, contact name/
                  title only) + the versioned service catalog; stable evidence ids.
  3. BUDGET       analyses per run, tokens per run, USD per run / month (checked before a call).
  4. MODEL        ``ReasoningProvider.complete`` (OpenAI Responses API: strict JSON schema,
                  token + cost budgets, typed errors).
  5. VALIDATE     strict schema; evidence ids must exist in the bundle; offer must exist in
                  the catalog; hypotheses never as facts. Failures -> REJECTED_OUTPUT record.
  6. PERSIST      immutable analysis record with model, gateway, usage, cost, versions.
Never calls discovery, crawling, enrichment or any vendor SDK directly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from app.core.config import Settings
from app.core.errors import EnrichmentNotAllowedError, ProviderError, ProviderNotConfiguredError
from app.core.logging import get_logger
from app.domain.analysis.bundle import AnalysisBundle, AnalysisInput, build_bundle
from app.domain.analysis.catalog import ServiceCatalog, default_catalog
from app.domain.analysis.config import ANALYSIS_POLICY_VERSION, PROMPT_VERSION, AnalysisConfig
from app.domain.analysis.output import (
    AnalysisOutputError,
    LeadAnalysisOutput,
    parse_output,
    validate_output,
)
from app.domain.analysis.prompt import build_messages
from app.domain.analysis.records import AnalysisRunRecord, AnalysisStatus, LeadAnalysisRecord
from app.domain.enrichment.records import EnrichmentContactRecord
from app.domain.enums import ContactStatus, RunStatus
from app.domain.models import utcnow
from app.domain.qualification.icp import IcpConfig
from app.domain.qualification.records import QualificationDecisionRecord
from app.providers.base import ReasoningProvider
from app.providers.contracts import ChatMessage, ChatRequest, ChatResult
from app.repositories.analysis import AnalysisStore
from app.repositories.enrichment import EnrichmentStore
from app.repositories.qualification import QualificationStore
from app.services.enrichment_gate import ensure_enrichment_allowed

logger = get_logger(__name__)


def month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class LeadAnalysisService:
    def __init__(
        self,
        store: AnalysisStore,
        qualifications: QualificationStore,
        enrichment: EnrichmentStore,
        provider: ReasoningProvider,
        settings: Settings,
        *,
        catalog: ServiceCatalog | None = None,
        persist: bool = True,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._store = store
        self._qualifications = qualifications
        self._enrichment = enrichment
        self._provider = provider
        self._config = AnalysisConfig.from_settings(settings)
        self._icp = IcpConfig.from_settings(settings)
        self._catalog = catalog or default_catalog()
        self._persist = persist
        self._clock = clock

    @property
    def config(self) -> AnalysisConfig:
        return self._config

    async def _write(self, fn: Callable[..., None], *args: object) -> None:
        if self._persist:
            await asyncio.to_thread(fn, *args)

    def _record(
        self,
        run: AnalysisRunRecord,
        company_id: UUID,
        name: str,
        status: AnalysisStatus,
        **kw: object,
    ) -> LeadAnalysisRecord:
        return LeadAnalysisRecord(
            run_id=run.id,
            company_id=company_id,
            company_name=name,
            status=status,
            catalog_version=self._catalog.version,
            policy_version=ANALYSIS_POLICY_VERSION,
            prompt_version=PROMPT_VERSION,
            analyzed_at=self._clock(),
            **kw,  # type: ignore[arg-type]
        )

    # ------------------------------------------------------------------ run
    async def run(
        self, company_ids: list[UUID] | None = None, *, limit: int = 5
    ) -> tuple[AnalysisRunRecord, list[LeadAnalysisRecord]]:
        now = self._clock()
        ids: list[UUID] = (
            list(dict.fromkeys(company_ids))
            if company_ids
            else await asyncio.to_thread(self._store.eligible_company_ids, limit)
        )
        run = AnalysisRunRecord(
            mode=self._provider.status().mode,
            policy_version=ANALYSIS_POLICY_VERSION,
            prompt_version=PROMPT_VERSION,
            catalog_version=self._catalog.version,
            config=self._config.snapshot(),
            started_at=now,
        )
        await self._write(self._store.create_run, run)
        month_cost = await asyncio.to_thread(self._store.month_cost, month_start(now))
        records: list[LeadAnalysisRecord] = []
        try:
            for company_id in ids:
                reason = self._budget_reason(run, month_cost)
                if reason is not None:
                    run.error_code, run.error_message = "budget_exhausted", reason
                    break
                record = await self.analyze_company(run, company_id)
                records.append(record)
                await self._write(self._store.insert_analysis, record)
                self._count(run, record)
        except BaseException as exc:
            cancelled = not isinstance(exc, Exception)
            run.status = RunStatus.CANCELLED if cancelled else RunStatus.FAILED
            run.error_code = "cancelled" if cancelled else getattr(exc, "code", "internal_error")
            run.error_message = str(exc)[:500] or type(exc).__name__
            run.finished_at = self._clock()
            if self._persist:
                self._store.finish_run(run)
            raise
        run.status = RunStatus.SUCCEEDED
        run.finished_at = self._clock()
        await self._write(self._store.finish_run, run)
        return run, records

    def _budget_reason(self, run: AnalysisRunRecord, month_cost: Decimal) -> str | None:
        cfg = self._config
        if run.analyses_created - run.blocked_count >= cfg.max_analyses_per_run:
            return f"analysis limit per run reached ({cfg.max_analyses_per_run})"
        if run.total_tokens >= cfg.max_tokens_per_run:
            return f"token budget per run reached ({cfg.max_tokens_per_run})"
        if run.cost_usd > cfg.max_cost_usd_per_run:
            return f"cost budget per run exceeded (${cfg.max_cost_usd_per_run})"
        if month_cost + run.cost_usd > cfg.max_cost_usd_per_month:
            return f"monthly cost budget exceeded (${cfg.max_cost_usd_per_month})"
        return None

    @staticmethod
    def _count(run: AnalysisRunRecord, r: LeadAnalysisRecord) -> None:
        run.analyses_created += 1
        if r.status is AnalysisStatus.SUCCEEDED:
            run.succeeded_count += 1
        elif r.status is AnalysisStatus.REJECTED_OUTPUT:
            run.rejected_count += 1
        elif r.status is AnalysisStatus.BLOCKED:
            run.blocked_count += 1
        elif r.status is AnalysisStatus.FAILED:
            run.failed_count += 1
        run.prompt_tokens += r.usage.get("prompt_tokens", 0)
        run.completion_tokens += r.usage.get("completion_tokens", 0)
        run.total_tokens += r.usage.get("total_tokens", 0)
        run.cost_usd += r.cost_usd or Decimal(0)

    # ------------------------------------------------------------------ eligibility
    def _contact(
        self, company_id: UUID, decision: QualificationDecisionRecord
    ) -> EnrichmentContactRecord | None:
        usable = (
            {ContactStatus.VERIFIED}
            if self._config.require_verified_contact
            else {ContactStatus.VERIFIED, ContactStatus.UNVERIFIED}
        )
        candidates = [
            c
            for c in self._enrichment.contacts(company_id)  # newest first
            if c.qualification_id == decision.id and c.status in usable and c.full_name
        ]
        return candidates[0] if candidates else None

    async def analyze_company(self, run: AnalysisRunRecord, company_id: UUID) -> LeadAnalysisRecord:
        ctx = await asyncio.to_thread(self._enrichment.load_company, company_id)
        try:
            decision = await asyncio.to_thread(
                ensure_enrichment_allowed, self._qualifications, company_id, self._icp
            )
        except EnrichmentNotAllowedError as exc:
            return self._record(
                run,
                company_id,
                ctx.name,
                AnalysisStatus.BLOCKED,
                qualification_id=None,
                contact_id=None,
                error_code=exc.code,
                error_message=exc.message,
            )
        contact = await asyncio.to_thread(self._contact, company_id, decision)
        if contact is None or ctx.domain is None:
            return self._record(
                run,
                company_id,
                ctx.name,
                AnalysisStatus.BLOCKED,
                qualification_id=decision.id,
                contact_id=None,
                error_code="no_usable_contact",
                error_message=(
                    "no VERIFIED Phase 6 contact enriched under the latest QUALIFIED decision"
                ),
            )
        data = await asyncio.to_thread(self._qualifications.load, company_id)
        bundle = build_bundle(
            AnalysisInput(
                company_id=company_id,
                company_name=ctx.name,
                domain=ctx.domain,
                decision=decision,
                contact=contact,
                signals=data.signals,
                pages=list(ctx.pages),
            ),
            self._config,
        )
        return await self._analyze(run, company_id, ctx.name, decision, contact, bundle)

    # ------------------------------------------------------------------ model + validation
    async def _analyze(
        self,
        run: AnalysisRunRecord,
        company_id: UUID,
        name: str,
        decision: QualificationDecisionRecord,
        contact: EnrichmentContactRecord,
        bundle: AnalysisBundle,
    ) -> LeadAnalysisRecord:
        common: dict[str, object] = {
            "qualification_id": decision.id,
            "contact_id": contact.id,
            "evidence_bundle": bundle.payload,
            "evidence_signal_ids": list(bundle.signal_ids),
        }
        request = ChatRequest(
            messages=[ChatMessage(**m) for m in build_messages(bundle, self._catalog)],
            purpose="lead_analysis",
            response_schema=LeadAnalysisOutput.model_json_schema(),
            response_schema_name="lead_analysis",
        )
        try:
            result = await self._provider.complete(request)
        except ProviderError as exc:
            logger.warning("lead analysis model call failed", extra={"error_code": exc.code})
            if isinstance(exc, ProviderNotConfiguredError):
                raise  # systemic (key/scope/base URL): stop the run, do not burn through leads
            return self._record(
                run,
                company_id,
                name,
                AnalysisStatus.FAILED,
                error_code=exc.code,
                error_message=exc.message,
                **common,
                **self._spent(exc),
            )
        meta = self._meta(result)
        try:
            output = parse_output(result.content)
            validate_output(output, bundle, self._catalog)
            if output.confidence < self._config.min_confidence:
                raise AnalysisOutputError(
                    "low_confidence",
                    [f"confidence {output.confidence} < minimum {self._config.min_confidence}"],
                )
        except AnalysisOutputError as exc:
            return self._record(
                run,
                company_id,
                name,
                AnalysisStatus.REJECTED_OUTPUT,
                error_code=exc.kind,
                error_message="; ".join(exc.problems)[:1000],
                raw_excerpt=result.content[:500],
                **common,
                **meta,
            )
        return self._record(
            run,
            company_id,
            name,
            AnalysisStatus.SUCCEEDED,
            analysis=output.model_dump(mode="json"),
            recommended_offer_id=output.recommended_offer_id,
            primary_trigger=output.primary_trigger,
            confidence=output.confidence,
            supporting_evidence_ids=output.supporting_evidence_ids,
            **common,
            **meta,
        )

    @staticmethod
    def _spent(exc: ProviderError) -> dict[str, object]:
        """Usage/cost of a call that was billed but unusable (refusal, incomplete output):
        recorded so budgets stay honest."""
        usage = exc.details.get("usage")
        if not isinstance(usage, dict):
            return {}
        cost = exc.details.get("cost_usd")
        return {
            "usage": {str(k): int(v) for k, v in usage.items()},
            "cost_usd": Decimal(str(cost)) if cost is not None else None,
            "model": exc.details.get("model"),
            "gateway": exc.details.get("provider"),
            "provider": exc.details.get("provider"),
        }

    @staticmethod
    def _meta(result: ChatResult) -> dict[str, object]:
        return {
            "model": result.model,
            "gateway": result.gateway,
            "provider": result.upstream_provider,
            "usage": result.usage.model_dump(),
            "latency_ms": result.latency_ms,
            "cost_usd": result.cost_usd,
            "attempts": result.attempts,
        }
