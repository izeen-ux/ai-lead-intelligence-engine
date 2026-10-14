"""Enrichment orchestration (Phase 6) - QUALIFIED companies only.

``EnrichmentService.run(...)`` per company:
  1. GATE      ``ensure_enrichment_allowed`` (latest decision QUALIFIED, current policy version
               and ICP config). Refused -> a BLOCKED record, zero provider calls.
  2. IDENTITY  decision-makers from the company's own pages (+ optional Hunter Domain Search);
               ambiguous / missing identities are recorded and never sent to a provider.
  3. CACHE     fresh cached result with fresh verification -> reused, zero calls; fresh result
               with stale verification -> re-verify only.
  4. BUDGET    per-lead / per-run / per-month / sidecar-call limits checked BEFORE each call.
  5. WATERFALL enrichment-kit (the single entry point): Hunter, then Apollo if needed.
  6. MERGE     person/domain matching, Hunter-first merge with per-field provenance.
  7. VERIFY    Hunter verification of the final email unless a fresh Hunter verification came
               with the finder result. Unverified results are stored as UNVERIFIED.
  8. PERSIST   append-only attempts + contacts, cache upsert, run totals.
It never calls Apify, Firecrawl, Crawl4AI, Agent-Reach, Laya or the LLM reviewer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from app.core.config import Settings
from app.core.errors import (
    EnrichmentNotAllowedError,
    ProviderError,
    ProviderNotConfiguredError,
    ProviderUnavailableError,
)
from app.core.logging import get_logger
from app.domain.enrichment.budget import BudgetLedger
from app.domain.enrichment.config import ENRICHMENT_POLICY_VERSION, EnrichmentConfig
from app.domain.enrichment.identity import (
    PersonCandidate,
    PersonIdentity,
    candidates_from_search,
    candidates_from_web_search,
    extract_people,
    linkedin_from_pages,
    merge_candidates,
    normalize_name,
    resolve_identities,
)
from app.domain.enrichment.policy import (
    assess,
    final_status,
    merge,
    preverified,
    verification_fresh,
)
from app.domain.enrichment.records import (
    CacheEntry,
    EnrichmentContactRecord,
    EnrichmentRunRecord,
    ProviderAttemptRecord,
)
from app.domain.enums import (
    AttemptStatus,
    ContactStatus,
    EmailVerificationStatus,
    EnrichmentStatus,
    EnrichmentTarget,
    PhoneStatus,
    ProviderName,
    ProviderOperation,
    RunStatus,
)
from app.domain.models import EnrichmentResult, utcnow
from app.domain.qualification.icp import IcpConfig
from app.domain.qualification.records import QualificationDecisionRecord
from app.providers.base import EmailVerifier, EnrichmentOrchestrator, WebSearchProvider
from app.providers.contracts import EnrichmentRequest, PeopleSearchRequest, WebSearchQuery
from app.repositories.enrichment import CompanyContext, EnrichmentStore
from app.repositories.qualification import QualificationStore
from app.services.enrichment_gate import ensure_enrichment_allowed

logger = get_logger(__name__)

_ATTEMPT_STATUS = {
    EnrichmentStatus.SUCCESS: AttemptStatus.HIT,
    EnrichmentStatus.PARTIAL: AttemptStatus.HIT,
    EnrichmentStatus.NOT_FOUND: AttemptStatus.MISS,
    EnrichmentStatus.FAILED: AttemptStatus.ERROR,
    EnrichmentStatus.SKIPPED: AttemptStatus.SKIPPED,
}
_OPERATION = {
    ProviderName.HUNTER: ProviderOperation.EMAIL_FINDER,
    ProviderName.APOLLO: ProviderOperation.PERSON_MATCH,
}
_INFRA_ERRORS = (ProviderUnavailableError, ProviderNotConfiguredError)


def month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class EnrichmentService:
    def __init__(
        self,
        store: EnrichmentStore,
        qualifications: QualificationStore,
        orchestrator: EnrichmentOrchestrator,
        verifier: EmailVerifier,
        settings: Settings,
        *,
        persist: bool = True,
        clock: Callable[[], datetime] = utcnow,
        web_search: WebSearchProvider | None = None,
    ) -> None:
        self._store = store
        self._web_search = web_search  # optional founder lookup in public search results
        self._web_searches = 0
        self._web_links: dict[str, tuple[str, str]] = {}  # person -> (profile url, result url)
        self._qualifications = qualifications
        self._orchestrator = orchestrator
        self._verifier = verifier
        self._config = EnrichmentConfig.from_settings(settings)
        self._icp = IcpConfig.from_settings(settings)
        self._persist = persist
        self._clock = clock
        self._seen: set[str] = set()

    @property
    def config(self) -> EnrichmentConfig:
        return self._config

    # ------------------------------------------------------------------ persistence helpers
    async def _write(self, fn: Callable[..., None], *args: object) -> None:
        if self._persist:
            await asyncio.to_thread(fn, *args)

    def _usd(self, provider: ProviderName, credits: Decimal) -> Decimal:
        price = (
            self._config.hunter_credit_usd
            if provider is ProviderName.HUNTER
            else self._config.apollo_credit_usd
            if provider is ProviderName.APOLLO
            else Decimal(0)
        )
        return price * credits

    async def _attempt(
        self,
        run: EnrichmentRunRecord,
        company_id: UUID,
        person_key: str | None,
        provider: ProviderName,
        operation: ProviderOperation,
        status: AttemptStatus,
        *,
        credits: Decimal = Decimal(0),
        error_code: str | None = None,
        http_status: int | None = None,
        latency_ms: int | None = None,
        detail: dict[str, object] | None = None,
    ) -> ProviderAttemptRecord:
        record = ProviderAttemptRecord(
            run_id=run.id,
            company_id=company_id,
            person_key=person_key,
            provider=provider,
            operation=operation,
            status=status,
            error_code=error_code,
            http_status=http_status,
            latency_ms=latency_ms,
            credits=credits,
            estimated_cost_usd=self._usd(provider, credits),
            detail=detail or {},
        )
        await self._write(self._store.insert_attempt, record)
        return record

    # ------------------------------------------------------------------ run
    async def run(
        self, company_ids: list[UUID] | None = None, *, limit: int = 5
    ) -> tuple[EnrichmentRunRecord, list[EnrichmentContactRecord]]:
        now = self._clock()
        self._seen = set()
        self._web_searches = 0
        ledger = BudgetLedger(
            self._config,
            month_credits=await asyncio.to_thread(self._store.month_credits, month_start(now)),
        )
        ids: list[UUID] = (
            list(company_ids)
            if company_ids
            else await asyncio.to_thread(self._store.qualified_company_ids, limit)
        )
        run = EnrichmentRunRecord(
            mode=self._orchestrator.status().mode,
            policy_version=ENRICHMENT_POLICY_VERSION,
            config=self._config.snapshot(),
            started_at=now,
        )
        await self._write(self._store.create_run, run)
        contacts: list[EnrichmentContactRecord] = []
        try:
            for company_id in ids:
                if ledger.exhausted():
                    run.error_code = "budget_exhausted"
                    run.error_message = "enrichment budget exhausted; remaining companies skipped"
                    break
                contacts += await self.enrich_company(run, company_id, ledger)
                run.companies_evaluated += 1
        except BaseException as exc:
            cancelled = not isinstance(exc, Exception)
            run.status = RunStatus.CANCELLED if cancelled else RunStatus.FAILED
            run.error_code = "cancelled" if cancelled else getattr(exc, "code", "internal_error")
            run.error_message = str(exc)[:500] or type(exc).__name__
            self._totals(run, contacts, ledger)
            if self._persist:
                self._store.finish_run(run)  # sync: the loop may be shutting down
            raise
        run.status = RunStatus.SUCCEEDED
        self._totals(run, contacts, ledger)
        await self._write(self._store.finish_run, run)
        return run, contacts

    def _totals(
        self,
        run: EnrichmentRunRecord,
        contacts: list[EnrichmentContactRecord],
        ledger: BudgetLedger,
    ) -> None:
        run.finished_at = self._clock()
        run.contacts_created = len(contacts)
        run.verified_count = sum(c.status is ContactStatus.VERIFIED for c in contacts)
        run.cached_count = sum(c.from_cache for c in contacts)
        run.blocked_count = sum(c.status is ContactStatus.BLOCKED for c in contacts)
        run.failed_count = sum(c.status is ContactStatus.FAILED for c in contacts)
        run.hunter_credits = ledger.run_credits.get(ProviderName.HUNTER, Decimal(0))
        run.apollo_credits = ledger.run_credits.get(ProviderName.APOLLO, Decimal(0))
        run.sidecar_calls = ledger.sidecar_calls
        run.estimated_cost_usd = ledger.run_usd

    # ------------------------------------------------------------------ company
    def _contact(
        self,
        run: EnrichmentRunRecord,
        ctx: CompanyContext,
        decision: QualificationDecisionRecord | None,
        status: ContactStatus,
        **kw: object,
    ) -> EnrichmentContactRecord:
        return EnrichmentContactRecord(
            run_id=run.id,
            company_id=ctx.company_id,
            company_name=ctx.name,
            domain=ctx.domain,
            qualification_id=decision.id if decision else None,
            status=status,
            policy_version=ENRICHMENT_POLICY_VERSION,
            **kw,  # type: ignore[arg-type]
        )

    async def enrich_company(
        self, run: EnrichmentRunRecord, company_id: UUID, ledger: BudgetLedger
    ) -> list[EnrichmentContactRecord]:
        ctx = await asyncio.to_thread(self._store.load_company, company_id)
        try:
            decision = await asyncio.to_thread(
                ensure_enrichment_allowed, self._qualifications, company_id, self._icp
            )
        except EnrichmentNotAllowedError as exc:
            blocked = self._contact(run, ctx, None, ContactStatus.BLOCKED, reasons=[exc.message])
            await self._write(self._store.insert_contact, blocked)
            logger.info(
                "enrichment blocked by qualification gate", extra={"company_id": str(company_id)}
            )
            return [blocked]
        domain = ctx.domain
        if domain is None:  # pragma: no cover - a QUALIFIED company always has a verified domain
            raise EnrichmentNotAllowedError("qualified company without a verified domain")

        candidates = extract_people(ctx.pages, ctx.name)
        resolution = resolve_identities(
            candidates,
            company_id=ctx.company_id,
            company_name=ctx.name,
            domain=domain,
            config=self._config,
        )
        if not resolution.selected and self._config.people_search == "hunter":
            found = await self._people_search(run, ctx, domain, ledger)
            resolution = resolve_identities(
                merge_candidates(candidates, found),
                company_id=ctx.company_id,
                company_name=ctx.name,
                domain=domain,
                config=self._config,
            )
        if not resolution.selected:
            found = await self._founder_web_search(ctx, domain)
            if found:
                candidates = merge_candidates(candidates, found)
                resolution = resolve_identities(
                    candidates,
                    company_id=ctx.company_id,
                    company_name=ctx.name,
                    domain=domain,
                    config=self._config,
                )
        skipped = [f"{name}: {why}" for name, why in resolution.skipped]
        if not resolution.selected:
            status = (
                ContactStatus.AMBIGUOUS
                if resolution.status == "ambiguous"
                else ContactStatus.NO_PERSON
            )
            record = self._contact(
                run,
                ctx,
                decision,
                status,
                reasons=skipped or ["no senior decision-maker found on the verified domain"],
            )
            await self._write(self._store.insert_contact, record)
            return [record]
        out = []
        for identity in resolution.selected:
            if identity.person_key in self._seen:
                continue  # duplicate suppression inside one run
            self._seen.add(identity.person_key)
            record = await self.enrich_person(run, ctx, decision, identity, ledger)
            record.reasons.extend(skipped)
            await self._write(self._store.insert_contact, record)
            out.append(record)
        return out

    async def _founder_web_search(self, ctx: CompanyContext, domain: str) -> list[PersonCandidate]:
        """Founder / CEO named in public search results for this company (budgeted; a search
        failure only means "nobody found")."""
        cfg = self._config
        if self._web_search is None or not cfg.web_search:
            return []
        if self._web_searches >= cfg.max_web_searches_per_run:
            return []
        self._web_searches += 1
        query = f'"{ctx.name}" founder OR "co-founder" OR CEO {domain}'
        try:
            result = await self._web_search.search_web(
                WebSearchQuery(query=query, limit=8, country=self._icp.country_code)
            )
        except ProviderError as exc:
            logger.warning("founder web search failed", extra={"error_code": exc.code})
            return []
        hits = [(h.url, f"{h.title or ''} . {h.description or ''}") for h in result.hits]
        people = candidates_from_web_search(hits, ctx.name)
        for person in people:
            if person.linkedin_url:
                self._web_links[person.key] = (person.linkedin_url, person.sources[0])
        return people

    async def _people_search(
        self, run: EnrichmentRunRecord, ctx: CompanyContext, domain: str, ledger: BudgetLedger
    ) -> list:  # type: ignore[type-arg]
        key = f"people:{domain}"
        reason = ledger.blocked_reason(ProviderName.HUNTER, key)
        if reason is not None:
            await self._attempt(
                run,
                ctx.company_id,
                None,
                ProviderName.HUNTER,
                ProviderOperation.DOMAIN_SEARCH,
                AttemptStatus.SKIPPED,
                detail={"reason": reason},
            )
            return []
        try:
            result = await self._orchestrator.find_people(
                PeopleSearchRequest(domain=domain, limit=5)
            )
        except ProviderError as exc:
            ledger.record(None, key, Decimal(0), sidecar_call=True)
            await self._attempt(
                run,
                ctx.company_id,
                None,
                ProviderName.HUNTER,
                ProviderOperation.DOMAIN_SEARCH,
                AttemptStatus.ERROR,
                error_code=exc.code,
            )
            if isinstance(exc, _INFRA_ERRORS):
                raise
            return []
        credits = Decimal(result.credits_used)
        ledger.record(ProviderName.HUNTER, key, credits, sidecar_call=True)
        await self._attempt(
            run,
            ctx.company_id,
            None,
            ProviderName.HUNTER,
            ProviderOperation.DOMAIN_SEARCH,
            AttemptStatus.HIT if result.people else AttemptStatus.MISS,
            credits=credits,
            error_code=result.error_code,
            detail={"people": len(result.people)},
        )
        return candidates_from_search(result.people, domain)

    # ------------------------------------------------------------------ person
    async def enrich_person(
        self,
        run: EnrichmentRunRecord,
        ctx: CompanyContext,
        decision: QualificationDecisionRecord,
        identity: PersonIdentity,
        ledger: BudgetLedger,
    ) -> EnrichmentContactRecord:
        now = self._clock()
        cfg = self._config
        base: dict[str, object] = {
            "person_key": identity.person_key,
            "full_name": identity.full_name,
            "first_name": identity.first_name,
            "last_name": identity.last_name,
            "title": identity.title,
            "role": identity.role,
            "role_rank": identity.role_rank,
            "identity": identity.as_record(),
        }
        cached = await asyncio.to_thread(self._store.cache_get, identity.cache_key)
        if cached is not None and cached.fresh(now):
            return await self._from_cache(run, ctx, decision, identity, cached, ledger, base)

        pk = identity.person_key
        providers = ledger.allowed_waterfall(pk)
        if not providers:
            reason = ledger.blocked_reason(ProviderName.HUNTER, pk) or "no provider within budget"
            return self._contact(
                run, ctx, decision, ContactStatus.BUDGET_EXHAUSTED, reasons=[reason], **base
            )
        # Optional phone capability: default OFF, own per-run budget, decided before the call.
        phone_skip = ledger.phone_status()
        request = EnrichmentRequest(
            lead_id=decision.id,
            target=EnrichmentTarget.PERSON,
            person_name=identity.full_name,
            first_name=identity.first_name,
            last_name=identity.last_name,
            title=identity.title,
            company_name=ctx.name,
            domain=identity.domain,
            min_confidence=cfg.min_confidence,
            early_exit_confidence=cfg.early_exit_confidence,
            providers=providers,
            include_phone=phone_skip is None,
        )
        if phone_skip is None:
            ledger.phone_lookups += 1
        try:
            outcome = await self._orchestrator.enrich(request)
        except ProviderError as exc:
            ledger.record(None, pk, Decimal(0), sidecar_call=True)
            await self._attempt(
                run,
                ctx.company_id,
                pk,
                ProviderName.ENRICHMENT_KIT,
                ProviderOperation.EMAIL_FINDER,
                AttemptStatus.ERROR,
                error_code=exc.code,
            )
            if isinstance(exc, _INFRA_ERRORS):
                raise
            return self._contact(
                run,
                ctx,
                decision,
                ContactStatus.FAILED,
                reasons=[f"enrichment failed: {exc.code}"],
                **base,
            )
        ledger.record(None, pk, Decimal(0), attempts=0, sidecar_call=True)
        credits: dict[str, Decimal] = {}
        results: dict[str, str] = {}
        for a in outcome.attempts:
            await self._record_waterfall_attempt(run, ctx.company_id, pk, a, ledger)
            credits[a.provider.value] = credits.get(a.provider.value, Decimal(0)) + (
                a.cost_credits or Decimal(0)
            )
            results[a.provider.value] = (
                f"{a.status.value}"
                + (f" {a.email}" if a.email else "")
                + (f" ({a.error_code})" if a.error_code else "")
            )

        attempts = list(outcome.attempts)
        assessment = assess(identity, attempts, cfg)
        tried = {a.provider for a in attempts}
        if (
            not assessment.accepted
            and ProviderName.APOLLO not in tried
            and ProviderName.APOLLO in ledger.allowed_waterfall(pk)
        ):
            # Hunter "succeeded" (so the waterfall exited early) but its result was rejected
            # (wrong person / domain): give the fallback provider its turn - still budgeted.
            follow_up = await self._follow_up(run, ctx.company_id, pk, request, ledger)
            for a in follow_up:
                credits[a.provider.value] = credits.get(a.provider.value, Decimal(0)) + (
                    a.cost_credits or Decimal(0)
                )
                results[a.provider.value] = a.status.value + (f" {a.email}" if a.email else "")
            attempts += follow_up
            assessment = assess(identity, attempts, cfg)
        draft = merge(identity, assessment)
        draft.provenance["waterfall"] = results
        if not draft.linkedin_url:
            # no provider returned one: a profile link the company's own page shows next to
            # this person's name is public metadata (the LinkedIn page is never fetched)
            site = linkedin_from_pages(identity.full_name, ctx.pages)
            if site is not None:
                draft.linkedin_url = site[0]
                draft.provenance["linkedin_url"] = f"website: {site[1]}"
        if not draft.linkedin_url and normalize_name(identity.full_name) in self._web_links:
            link, result_url = self._web_links[normalize_name(identity.full_name)]
            draft.linkedin_url = link
            draft.provenance["linkedin_url"] = f"web search result: {result_url}"
        phone_status = phone_skip or (PhoneStatus.FOUND if draft.phone else PhoneStatus.NOT_FOUND)
        draft.provenance["phone"] = {
            "status": phone_status.value,
            "provider": draft.phone_provider.value if draft.phone_provider else None,
        }
        reasons = [f"rejected {p}: {r}" for p, r in assessment.rejected]
        verification = EmailVerificationStatus.UNVERIFIED
        verified_at: datetime | None = None
        verification_provider: ProviderName | None = None
        if draft.email:
            pre = preverified(draft.best, now, cfg)
            if pre is not None:
                verification, verified_at = EmailVerificationStatus.VALID, pre
                verification_provider = ProviderName.HUNTER
                draft.provenance["verification"] = "hunter email-finder verification (fresh)"
            else:
                verification, verified_at, verification_provider, why = await self._verify(
                    run, ctx.company_id, pk, draft.email, ledger, credits
                )
                if why:
                    reasons.append(why)
        errored = all(a.status is EnrichmentStatus.FAILED for a in attempts)
        status = (
            ContactStatus.FAILED
            if not draft.email and attempts and errored
            else final_status(draft.email, verification, draft.confidence, cfg)
        )
        if status is ContactStatus.UNVERIFIED and verification is EmailVerificationStatus.VALID:
            reasons.append(f"confidence {draft.confidence} below accept threshold")
        record = self._contact(
            run,
            ctx,
            decision,
            status,
            email=draft.email,
            email_verification=verification if draft.email else EmailVerificationStatus.UNVERIFIED,
            verified_at=verified_at if draft.email else None,
            verification_provider=verification_provider,
            confidence=draft.confidence,
            provider=draft.provider,
            linkedin_url=draft.linkedin_url,
            phone=draft.phone,
            phone_status=phone_status,
            provenance=draft.provenance,
            reasons=reasons,
            credits_used={k: str(v) for k, v in credits.items()},
            estimated_cost_usd=sum(
                (self._usd(ProviderName(k), v) for k, v in credits.items()), Decimal(0)
            ),
            **{**base, "title": draft.title or identity.title},
        )
        if status is not ContactStatus.FAILED:
            await self._write(
                self._store.cache_put,
                CacheEntry(
                    cache_key=identity.cache_key,
                    company_id=ctx.company_id,
                    person_key=pk,
                    domain=identity.domain,
                    email=record.email,
                    email_verification=record.email_verification,
                    verified_at=record.verified_at,
                    confidence=record.confidence,
                    provider=record.provider,
                    title=record.title,
                    linkedin_url=record.linkedin_url,
                    phone=record.phone,
                    phone_status=record.phone_status,
                    provenance=record.provenance,
                    cached_at=now,
                    expires_at=now + timedelta(days=cfg.cache_ttl_days),
                ),
            )
        return record

    async def _follow_up(
        self,
        run: EnrichmentRunRecord,
        company_id: UUID,
        pk: str,
        request: EnrichmentRequest,
        ledger: BudgetLedger,
    ) -> list[EnrichmentResult]:
        try:
            outcome = await self._orchestrator.enrich(
                request.model_copy(update={"providers": [ProviderName.APOLLO]})
            )
        except ProviderError as exc:
            ledger.record(None, pk, Decimal(0), sidecar_call=True)
            await self._attempt(
                run, company_id, pk, ProviderName.ENRICHMENT_KIT, ProviderOperation.PERSON_MATCH,
                AttemptStatus.ERROR, error_code=exc.code,
            )  # fmt: skip
            if isinstance(exc, _INFRA_ERRORS):
                raise
            return []
        ledger.record(None, pk, Decimal(0), attempts=0, sidecar_call=True)
        for a in outcome.attempts:
            await self._record_waterfall_attempt(run, company_id, pk, a, ledger)
        return list(outcome.attempts)

    async def _record_waterfall_attempt(
        self,
        run: EnrichmentRunRecord,
        company_id: UUID,
        pk: str,
        a: EnrichmentResult,
        ledger: BudgetLedger,
    ) -> None:
        credits = a.cost_credits or Decimal(0)
        called = a.status is not EnrichmentStatus.SKIPPED
        ledger.record(a.provider, pk, credits, attempts=1 if called else 0)
        await self._attempt(
            run,
            company_id,
            pk,
            a.provider,
            _OPERATION.get(a.provider, ProviderOperation.EMAIL_FINDER),
            _ATTEMPT_STATUS[a.status],
            credits=credits,
            error_code=a.error_code,
            http_status=a.fields.get("http_status")
            if isinstance(a.fields.get("http_status"), int)
            else None,
            latency_ms=a.latency_ms,
            detail={"email_found": bool(a.email), "confidence": a.confidence},
        )

    async def _verify(
        self,
        run: EnrichmentRunRecord,
        company_id: UUID,
        pk: str,
        email: str,
        ledger: BudgetLedger,
        credits: dict[str, Decimal],
    ) -> tuple[EmailVerificationStatus, datetime | None, ProviderName | None, str | None]:
        reason = ledger.blocked_reason(ProviderName.HUNTER, pk)
        if reason is not None:
            await self._attempt(
                run,
                company_id,
                pk,
                ProviderName.HUNTER,
                ProviderOperation.EMAIL_VERIFIER,
                AttemptStatus.SKIPPED,
                detail={"reason": reason},
            )
            return EmailVerificationStatus.UNVERIFIED, None, None, f"verification skipped: {reason}"
        try:
            v = await self._verifier.verify_email(email)
        except ProviderError as exc:
            ledger.record(None, pk, Decimal(0), sidecar_call=True)
            await self._attempt(
                run,
                company_id,
                pk,
                ProviderName.HUNTER,
                ProviderOperation.EMAIL_VERIFIER,
                AttemptStatus.ERROR,
                error_code=exc.code,
            )
            if isinstance(exc, _INFRA_ERRORS):
                raise
            return (
                EmailVerificationStatus.UNVERIFIED,
                None,
                None,
                f"verification failed: {exc.code}",
            )
        spent = Decimal(0) if v.error_code else Decimal(1)
        ledger.record(ProviderName.HUNTER, pk, spent, sidecar_call=True)
        credits[ProviderName.HUNTER.value] = (
            credits.get(ProviderName.HUNTER.value, Decimal(0)) + spent
        )
        await self._attempt(
            run,
            company_id,
            pk,
            ProviderName.HUNTER,
            ProviderOperation.EMAIL_VERIFIER,
            AttemptStatus.ERROR if v.error_code else AttemptStatus.HIT,
            credits=spent,
            error_code=v.error_code,
            http_status=v.http_status,
            detail={"status": v.status.value},
        )
        why = f"verification error: {v.error_code}" if v.error_code else None
        return v.status, (v.checked_at if not v.error_code else None), v.provider, why

    async def _from_cache(
        self,
        run: EnrichmentRunRecord,
        ctx: CompanyContext,
        decision: QualificationDecisionRecord,
        identity: PersonIdentity,
        cached: CacheEntry,
        ledger: BudgetLedger,
        base: dict[str, object],
    ) -> EnrichmentContactRecord:
        now = self._clock()
        cfg = self._config
        verification, verified_at = cached.email_verification, cached.verified_at
        reasons = ["served from cache (no provider call)"]
        credits: dict[str, Decimal] = {}
        provider: ProviderName | None = None
        if cached.email and not verification_fresh(verified_at, now, cfg):
            # never present an expired verification as fresh: re-verify or downgrade
            verification, verified_at, provider, why = await self._verify(
                run, ctx.company_id, identity.person_key, cached.email, ledger, credits
            )
            reasons = [
                "cached result without a fresh verification -> re-verified"
                if verified_at is None or cached.verified_at is None
                else "cached result; verification expired -> re-verified"
            ]
            if why:
                reasons.append(why)
            await self._write(
                self._store.cache_put,
                cached.model_copy(
                    update={"email_verification": verification, "verified_at": verified_at}
                ),
            )
        else:
            await asyncio.to_thread(self._store.cache_hit, cached.cache_key)
        status = final_status(cached.email, verification, cached.confidence, cfg)
        return self._contact(
            run,
            ctx,
            decision,
            status,
            email=cached.email,
            email_verification=verification if cached.email else EmailVerificationStatus.UNVERIFIED,
            verified_at=verified_at if cached.email else None,
            verification_provider=provider or (ProviderName.HUNTER if verified_at else None),
            confidence=cached.confidence,
            provider=cached.provider,
            linkedin_url=cached.linkedin_url,
            phone=cached.phone,
            phone_status=cached.phone_status,
            from_cache=True,
            provenance={**cached.provenance, "cache": {"cached_at": cached.cached_at.isoformat()}},
            reasons=reasons,
            credits_used={k: str(v) for k, v in credits.items()},
            estimated_cost_usd=sum(
                (self._usd(ProviderName(k), v) for k, v in credits.items()), Decimal(0)
            ),
            **{**base, "title": cached.title or identity.title},
        )
