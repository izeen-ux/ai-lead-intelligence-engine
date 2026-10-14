"""Qualification gate orchestration (Phase 5).

``QualificationService.run(...)`` evaluates companies in this fixed order:

  1. identity/domain eligibility + 2. hard ICP rules   (deterministic, app.domain.qualification)
  3. event clustering of the Phase 4 signals           (duplicate coverage counts once)
  4. evidence bundle                                   (what Laya / the LLM see)
  5. Laya typed decisions                              (only when the hard rules allow a path)
  6. optional LLM review                               (REVIEW cases only; off by default)
  7. final decision policy                             (hard rules can never be overridden)
  8. persist an immutable decision with evidence ids   (history, never overwritten)

It never calls Apify, Firecrawl, Crawl4AI, Agent-Reach, Hunter, Apollo or enrichment-kit, and it
never starts enrichment: Phase 6 must call ``app.services.enrichment_gate`` first.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from app.core.config import Settings
from app.core.errors import ProviderError, ProviderNotConfiguredError
from app.core.logging import get_logger
from app.domain.enums import QualificationDecision, RunStatus, SignalType, StageStatus
from app.domain.models import utcnow
from app.domain.qualification.bundle import LAYA_QUESTIONS, EvidenceBundle, build_bundle, interpret
from app.domain.qualification.clustering import EventCluster, cluster_signals
from app.domain.qualification.icp import POLICY_VERSION, IcpConfig
from app.domain.qualification.policy import (
    AttributionOutcome,
    PreliminaryDecision,
    apply_attribution_review,
    apply_llm_review,
    decide,
    hard_gate,
)
from app.domain.qualification.records import QualificationDecisionRecord, QualificationRunRecord
from app.domain.qualification.rules import ATTRIBUTION_UNKNOWN, evaluate_hard_rules
from app.providers.base import LlmReviewProvider, QualificationProvider
from app.providers.contracts import LlmReviewRequest, LlmReviewResult, QualificationRequest
from app.repositories.qualification import QualificationStore

logger = get_logger(__name__)


_SYSTEMIC_AI_ERRORS = frozenset(
    {"provider_quota_exhausted", "provider_not_configured", "provider_call_blocked"}
)


def _needs_attribution(preliminary: PreliminaryDecision | None) -> bool:
    """A REVIEW that exists only because funding news matched the company by name."""
    return (
        preliminary is not None
        and preliminary.decision is QualificationDecision.REVIEW
        and any(ATTRIBUTION_UNKNOWN in reason for reason in preliminary.reasons)
    )


class QualificationService:
    def __init__(
        self,
        store: QualificationStore,
        laya: QualificationProvider,
        settings: Settings,
        *,
        reviewer: LlmReviewProvider | None = None,
        persist: bool = True,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._store = store
        self._laya = laya
        self._reviewer = reviewer if settings.llm_review_enabled else None
        self._reviewer_down: ProviderError | None = None  # key / credits problem seen this run
        self._icp = IcpConfig.from_settings(settings)
        self._persist = persist
        self._clock = clock

    @property
    def icp(self) -> IcpConfig:
        return self._icp

    def _llm_mode(self) -> str:
        return "disabled" if self._reviewer is None else self._reviewer.status().mode

    async def run(
        self, company_ids: list[UUID] | None = None, *, limit: int = 5
    ) -> tuple[QualificationRunRecord, list[QualificationDecisionRecord]]:
        ids: list[UUID] = (
            list(company_ids)
            if company_ids
            else await asyncio.to_thread(self._store.candidate_ids, limit)
        )
        run = QualificationRunRecord(
            policy_version=POLICY_VERSION,
            laya_mode=self._laya.status().mode,
            llm_review_mode=self._llm_mode(),
            icp_config=self._icp.snapshot(),
            started_at=self._clock(),
        )
        if self._persist:
            await asyncio.to_thread(self._store.create_run, run)
        decisions: list[QualificationDecisionRecord] = []
        try:
            for company_id in ids:
                record = await self.qualify(run.id, company_id)
                decisions.append(record)
                run.count(record.decision)
        except Exception as exc:
            run.status = RunStatus.FAILED
            run.error_code = getattr(exc, "code", "internal_error")
            run.error_message = str(exc)[:500] or type(exc).__name__
            run.finished_at = self._clock()
            if self._persist:
                await asyncio.to_thread(self._store.finish_run, run)
            raise
        except BaseException as exc:
            run.status = RunStatus.CANCELLED
            run.error_code = "cancelled"
            run.error_message = type(exc).__name__
            run.finished_at = self._clock()
            if self._persist:
                self._store.finish_run(run)  # sync: the loop may be shutting down
            raise
        run.status = RunStatus.SUCCEEDED
        run.finished_at = self._clock()
        if self._persist:
            await asyncio.to_thread(self._store.finish_run, run)
        return run, decisions

    async def qualify(self, run_id: UUID, company_id: UUID) -> QualificationDecisionRecord:
        now = self._clock()
        data = await asyncio.to_thread(self._store.load, company_id)
        icp = self._icp
        clusters = cluster_signals(
            data.signals,
            company_name=data.facts.name,
            title_similarity=icp.cluster_title_similarity,
            window_days=icp.cluster_window_days,
        )
        hard = evaluate_hard_rules(data.facts, clusters, icp, now)
        bundle = build_bundle(data.facts, hard, clusters, icp, now)

        laya_status, laya_record, laya_error = StageStatus.SKIPPED, None, None
        laya_model = None
        preliminary = hard_gate(hard, icp)
        # Funding news that only matches by NAME: ask the AI reviewer whether it is about this
        # company. Confirmed events are re-checked by the same hard rules; nothing is waved
        # through.
        ai_reasons: list[str] = []
        ai_status, ai_record, ai_final = StageStatus.SKIPPED, None, None
        if self._reviewer is not None and _needs_attribution(preliminary):
            ai_status, ai_record, outcome = await self._attribution(
                company_id, data.profile, bundle, clusters, now
            )
            ai_reasons = outcome.reasons
            ai_final = outcome.decision
            if outcome.confirmed:
                for cluster in clusters:
                    if cluster.cluster_id in outcome.confirmed:
                        cluster.attributed = True
                hard = evaluate_hard_rules(data.facts, clusters, icp, now)
                bundle = build_bundle(data.facts, hard, clusters, icp, now)
                preliminary = hard_gate(hard, icp)
        if preliminary is None:
            assessment = None
            try:
                result = await self._laya.evaluate(
                    QualificationRequest(
                        subject_id=company_id,
                        state=bundle.for_laya(),
                        questions=list(LAYA_QUESTIONS),
                    )
                )
                assessment = interpret(result, icp)
                laya_status, laya_record = StageStatus.OK, assessment.as_record()
                laya_model = result.model_version
            except ProviderError as exc:
                laya_status, laya_error = StageStatus.ERROR, exc.code
                laya_record = {"error_code": exc.code, "error": exc.message}
                logger.warning(
                    "laya failed; decision falls back to REVIEW",
                    extra={"company_id": str(company_id), "error_code": exc.code},
                )
            preliminary = decide(hard, assessment, icp, laya_error=laya_error)

        if ai_final is not None:  # the AI found the evidence belongs to another company
            final, reasons = ai_final, [*preliminary.reasons, *ai_reasons]
            llm_status, llm_record = ai_status, ai_record
        elif ai_record is not None:  # one AI call per decision: no second opinion on top
            final, reasons = preliminary.decision, [*preliminary.reasons, *ai_reasons]
            llm_status, llm_record = ai_status, ai_record
        else:
            final, reasons, llm_status, llm_record = await self._review(
                company_id, preliminary, bundle, data.profile
            )
        record = QualificationDecisionRecord(
            run_id=run_id,
            company_id=company_id,
            company_name=data.facts.name,
            decision=final,
            path=hard.path,
            hard_rules_passed=hard.passed,
            hard_rule_results=[c.as_dict() for c in hard.checks],
            reasons=reasons,
            laya_status=laya_status,
            laya_result=laya_record,
            laya_confidence=preliminary.confidence,
            laya_model_version=laya_model,
            llm_review_status=llm_status,
            llm_review=llm_record,
            score=preliminary.score,
            confidence=preliminary.confidence,
            policy_version=POLICY_VERSION,
            icp_config=icp.snapshot(),
            evidence_bundle=bundle.as_record(),
            evidence_signal_ids=[UUID(i) for i in bundle.signal_ids],
            signal_count=len(data.signals),
            event_count=len(clusters),
            decided_at=now,
        )
        if self._persist:
            await asyncio.to_thread(self._store.insert_decision, record)
        logger.info(
            "qualification decided",
            extra={
                "company_id": str(company_id),
                "decision": final.value,
                "path": hard.path.value,
                "laya": laya_status.value,
                "llm": llm_status.value,
            },
        )
        return record

    async def _ask(self, request: LlmReviewRequest) -> LlmReviewResult:
        """One AI review call. A key or credit problem will not fix itself within a run, so
        after the first such error the remaining cases are not sent (they are retried by a
        later run) instead of failing one by one."""
        if self._reviewer is None:  # pragma: no cover - callers check
            raise ProviderNotConfiguredError("llm_review", "AI review is not enabled")
        if self._reviewer_down is not None:
            raise self._reviewer_down
        try:
            return await self._reviewer.review(request)
        except ProviderError as exc:
            if exc.code in _SYSTEMIC_AI_ERRORS:
                self._reviewer_down = exc
                logger.warning("ai review switched off for this run", extra={"error": exc.code})
            raise

    async def _attribution(
        self,
        company_id: UUID,
        profile: dict[str, str],
        bundle: EvidenceBundle,
        clusters: list[EventCluster],
        now: datetime,
    ) -> tuple[StageStatus, dict[str, object] | None, AttributionOutcome]:
        window = self._icp.funding_window_days
        candidates = sorted(
            c.cluster_id
            for c in clusters
            if SignalType.FUNDING in c.types
            and not c.attributed
            and c.event_date is not None
            and 0 <= (now - c.event_date).days <= window
            and c.cluster_id in set(bundle.event_ids)
        )
        if not candidates or self._reviewer is None:
            return StageStatus.SKIPPED, None, AttributionOutcome()
        try:
            review = await self._ask(
                LlmReviewRequest(
                    subject_id=company_id,
                    bundle=bundle.as_record(),
                    allowed_evidence_ids=candidates,
                    hard_rule_summary=[c.as_dict() for c in bundle.hard.checks],
                    qualified_allowed=True,
                    task="attribution",
                    company_profile=profile,
                    question_event_ids=candidates,
                )
            )
        except ProviderError as exc:
            return (
                StageStatus.ERROR,
                {"task": "attribution", "error_code": exc.code, "error": exc.message},
                AttributionOutcome(
                    reasons=[f"ai review unavailable ({exc.code}) - human decision"]
                ),
            )
        outcome = apply_attribution_review(review, set(candidates), self._icp)
        record: dict[str, object] = {
            **review.model_dump(mode="json"),
            "task": "attribution",
            "accepted": bool(outcome.confirmed or outcome.decision),
            "confirmed_event_ids": outcome.confirmed,
        }
        return StageStatus.OK, record, outcome

    async def _review(
        self,
        company_id: UUID,
        preliminary: PreliminaryDecision,
        bundle: EvidenceBundle,
        profile: dict[str, str] | None = None,
    ) -> tuple[QualificationDecision, list[str], StageStatus, dict[str, object] | None]:
        reasons = list(preliminary.reasons)
        if preliminary.decision is not QualificationDecision.REVIEW or self._reviewer is None:
            return preliminary.decision, reasons, StageStatus.SKIPPED, None
        allowed_ids = set(bundle.signal_ids) | set(bundle.event_ids)
        try:
            review = await self._ask(
                LlmReviewRequest(
                    subject_id=company_id,
                    bundle=bundle.as_record(),
                    allowed_evidence_ids=sorted(allowed_ids),
                    hard_rule_summary=[c.as_dict() for c in bundle.hard.checks],
                    qualified_allowed=preliminary.qualified_allowed,
                    company_profile=profile or {},
                )
            )
        except ProviderError as exc:
            reasons.append(f"llm review unavailable ({exc.code}) - human decision")
            return (
                QualificationDecision.REVIEW,
                reasons,
                StageStatus.ERROR,
                {"error_code": exc.code, "error": exc.message},
            )
        outcome = apply_llm_review(preliminary, review, self._icp, allowed_ids)
        record: dict[str, object] = {
            **review.model_dump(mode="json"),
            "accepted": outcome.accepted,
        }
        return outcome.decision, reasons + outcome.reasons, StageStatus.OK, record
