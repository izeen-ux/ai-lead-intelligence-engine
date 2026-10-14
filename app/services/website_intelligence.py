"""Website intelligence orchestration (Phase 3).

For one company:
  1. RESOLVE the official domain (Firecrawl Search -> scored candidates) unless it is already
     verified or an operator override is given. Ambiguous -> REVIEW, nothing is crawled.
  2. CRAWL within the page budget: Firecrawl first; Crawl4AI fallback only when Firecrawl
     failed or returned insufficient content (or when explicitly forced for a smoke test).
  3. VERIFY identity: the crawled homepage must name the company. Otherwise -> REVIEW and the
     domain is not attached (so it can never drive a merge).
  4. ASSIGN the verified domain; if another company already owns it, merge safely (see
     ``app/repositories/website.py``).

Spend: a run-level ``CreditBudget`` is checked before every paid call; per-company caps are
enforced again inside the adapters.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from uuid import UUID

from app.core.config import Settings
from app.core.errors import ProviderError
from app.core.logging import get_logger
from app.domain.domain_resolution import build_search_query, decide
from app.domain.enums import CrawlStatus, DomainResolutionStatus
from app.domain.models import DomainResolutionRecord, WebsiteCrawlRecord, utcnow
from app.domain.website import crawlable_domain, mentions_company
from app.providers.base import WebSearchProvider, WebsiteIntelligenceProvider
from app.providers.contracts import (
    WebSearchQuery,
    WebsiteExtractionRequest,
    WebsiteExtractionResult,
)
from app.repositories.website import CompanyContext, WebsiteStore

logger = get_logger(__name__)


def search_credit_estimate(limit: int) -> int:
    """Firecrawl Search: 2 credits per 10 results (the only search provider in Phase 3)."""
    return 2 * math.ceil(limit / 10)


@dataclass
class CreditBudget:
    """Run-level budget for paid website credits (checked before each paid call)."""

    total: int
    spent: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.spent)

    def can_afford(self, credits: int) -> bool:
        return credits <= self.remaining

    def spend(self, credits: int) -> None:
        self.spent += max(0, credits)


@dataclass
class _Attempt:
    record: WebsiteCrawlRecord
    result: WebsiteExtractionResult | None


@dataclass
class CompanyWebsiteOutcome:
    company_id: UUID
    domain_status: DomainResolutionStatus
    domain: str | None = None
    confidence: float | None = None
    reason: str | None = None
    crawl_status: CrawlStatus | None = None
    provider_used: str | None = None
    used_fallback: bool = False
    pages_stored: int = 0
    credits_used: int = 0
    surviving_company_id: UUID | None = None
    merged_from: UUID | None = None
    attempts: list[str] = field(default_factory=list)


class WebsiteIntelligenceService:
    def __init__(
        self,
        *,
        search: WebSearchProvider,
        primary: WebsiteIntelligenceProvider,
        fallback: WebsiteIntelligenceProvider | None,
        store: WebsiteStore,
        settings: Settings,
    ) -> None:
        self._search = search
        self._primary = primary
        self._fallback = fallback
        self._store = store
        self._settings = settings

    # ------------------------------------------------------------------ public
    async def process(
        self,
        company_id: UUID,
        budget: CreditBudget,
        *,
        domain_override: str | None = None,
        page_budget: int | None = None,
        force_fallback: bool = False,
        country: str | None = "IN",
    ) -> CompanyWebsiteOutcome:
        ctx = await asyncio.to_thread(self._store.company_context, company_id)
        start_spent = budget.spent
        outcome = CompanyWebsiteOutcome(company_id=company_id, domain_status=ctx.domain_status)

        domain, confidence, source = await self._resolve(
            ctx, budget, outcome, domain_override=domain_override, country=country
        )
        outcome.credits_used = budget.spent - start_spent
        if domain is None:
            return outcome

        pages = min(
            page_budget or self._settings.website_max_pages_per_company,
            self._settings.website_max_pages_per_company,
        )
        attempts = await self._crawl(ctx, domain, pages, budget, force_fallback)
        outcome.credits_used = budget.spent - start_spent
        chosen = self._choose(attempts)
        verified = (
            chosen is not None
            and bool(chosen.result)
            and mentions_company(chosen.result.pages if chosen.result else [], ctx.normalized_name)
        )
        for attempt in attempts:
            if attempt is chosen:
                attempt.record.identity_verified = verified
            outcome.attempts.append(
                f"{attempt.record.provider.value}:{attempt.record.status.value}"
            )
            await asyncio.to_thread(
                self._store.record_crawl,
                attempt.record,
                attempt.result.pages if (attempt is chosen and attempt.result) else [],
            )

        if chosen is not None and chosen.result is not None:
            outcome.crawl_status = chosen.record.status
            outcome.provider_used = chosen.record.provider.value
            outcome.used_fallback = chosen.record.is_fallback
            outcome.pages_stored = len(chosen.result.pages)

        if not verified:
            reason = (
                "homepage does not name the company (identity not confirmed)"
                if chosen is not None and chosen.result and chosen.result.pages
                else "no usable website content"
            )
            outcome.domain_status = DomainResolutionStatus.REVIEW
            outcome.reason = reason
            await asyncio.to_thread(
                self._store.set_domain_status, company_id, DomainResolutionStatus.REVIEW, confidence
            )
            return outcome

        assigned = await asyncio.to_thread(
            self._store.assign_verified_domain,
            company_id,
            domain,
            confidence,
            source,
            {"confidence": confidence, "source": source, "verified_by": "homepage_mentions_name"},
        )
        outcome.domain_status = DomainResolutionStatus.VERIFIED
        outcome.surviving_company_id = assigned.company_id
        outcome.merged_from = assigned.merged_from
        return outcome

    # ------------------------------------------------------------------ resolve
    async def _resolve(
        self,
        ctx: CompanyContext,
        budget: CreditBudget,
        outcome: CompanyWebsiteOutcome,
        *,
        domain_override: str | None,
        country: str | None,
    ) -> tuple[str | None, float, str]:
        if ctx.domain and ctx.domain_status is DomainResolutionStatus.VERIFIED:
            outcome.domain, outcome.confidence = ctx.domain, 1.0
            return ctx.domain, 1.0, "existing"

        if domain_override:
            try:
                domain = crawlable_domain(domain_override)
            except ValueError as exc:
                outcome.domain_status, outcome.reason = DomainResolutionStatus.REVIEW, str(exc)
                return None, 0.0, "operator_override"
            await asyncio.to_thread(
                self._store.record_resolution,
                DomainResolutionRecord(
                    company_id=ctx.company_id,
                    provider="operator",
                    query="(operator override)",
                    status=DomainResolutionStatus.RESOLVED,
                    chosen_domain=domain,
                    confidence=1.0,
                    reason="operator-supplied domain; must still pass homepage verification",
                ),
            )
            outcome.domain, outcome.confidence = domain, 1.0
            outcome.domain_status = DomainResolutionStatus.RESOLVED
            return domain, 1.0, "operator_override"

        limit = self._settings.domain_search_result_limit
        estimate = search_credit_estimate(limit)
        if not budget.can_afford(estimate):
            outcome.reason = "run credit budget exhausted before domain search"
            return None, 0.0, "firecrawl_search"
        query = build_search_query(ctx.name, country)
        try:
            found = await self._search.search_web(
                WebSearchQuery(query=query, limit=limit, country=country)
            )
        except ProviderError as exc:
            await asyncio.to_thread(
                self._store.record_resolution,
                DomainResolutionRecord(
                    company_id=ctx.company_id,
                    provider=self._search.name.value,
                    query=query,
                    status=DomainResolutionStatus.FAILED,
                    reason=exc.message,
                    error_code=exc.code,
                ),
            )
            await asyncio.to_thread(
                self._store.set_domain_status, ctx.company_id, DomainResolutionStatus.FAILED, None
            )
            outcome.domain_status, outcome.reason = DomainResolutionStatus.FAILED, exc.message
            return None, 0.0, "firecrawl_search"
        budget.spend(found.credits_used)

        decision = decide(
            ctx.normalized_name,
            found.hits,
            min_confidence=self._settings.domain_min_confidence,
            excluded_hosts=ctx.publisher_hosts,
        )
        await asyncio.to_thread(
            self._store.record_resolution,
            DomainResolutionRecord(
                company_id=ctx.company_id,
                provider=found.provider.value,
                query=query,
                status=decision.status,
                chosen_domain=decision.domain,
                confidence=decision.confidence,
                reason=decision.reason,
                candidates=[c.to_json() for c in decision.candidates]
                + [{"social_link": url} for url in decision.social_links.values()],
                credits_used=found.credits_used,
            ),
        )
        outcome.domain, outcome.confidence = decision.domain, decision.confidence
        outcome.domain_status, outcome.reason = decision.status, decision.reason
        if decision.status is not DomainResolutionStatus.RESOLVED or decision.domain is None:
            await asyncio.to_thread(
                self._store.set_domain_status, ctx.company_id, decision.status, decision.confidence
            )
            return None, decision.confidence, "firecrawl_search"
        try:
            domain = crawlable_domain(decision.domain)
        except ValueError as exc:
            outcome.domain_status, outcome.reason = DomainResolutionStatus.REVIEW, str(exc)
            await asyncio.to_thread(
                self._store.set_domain_status,
                ctx.company_id,
                DomainResolutionStatus.REVIEW,
                decision.confidence,
            )
            return None, decision.confidence, "firecrawl_search"
        await asyncio.to_thread(
            self._store.set_domain_status,
            ctx.company_id,
            DomainResolutionStatus.RESOLVED,
            decision.confidence,
        )
        return domain, decision.confidence, "firecrawl_search"

    # ------------------------------------------------------------------ crawl
    async def _attempt(
        self,
        provider: WebsiteIntelligenceProvider,
        ctx: CompanyContext,
        domain: str,
        pages: int,
        *,
        is_fallback: bool,
        max_credits: int | None,
    ) -> _Attempt:
        record = WebsiteCrawlRecord(
            company_id=ctx.company_id,
            domain=domain,
            provider=provider.name,
            is_fallback=is_fallback,
            status=CrawlStatus.FAILED,
            page_budget=pages,
        )
        try:
            result = await provider.extract_site(
                WebsiteExtractionRequest(domain=domain, page_budget=pages, max_credits=max_credits)
            )
        except ProviderError as exc:
            record.error_code, record.error_message = exc.code, exc.message
            record.finished_at = utcnow()
            return _Attempt(record, None)
        record.status = CrawlStatus.SUCCEEDED if result.sufficient else CrawlStatus.INSUFFICIENT
        record.sufficient = result.sufficient
        record.reason = result.reason
        record.pages_attempted = result.pages_attempted
        record.pages_succeeded = len(result.pages)
        record.credits_used = result.credits_used
        record.facts = result.facts
        record.social_links = result.social_links
        record.failed_urls = result.failed_urls
        record.finished_at = utcnow()
        return _Attempt(record, result)

    async def _crawl(
        self,
        ctx: CompanyContext,
        domain: str,
        pages: int,
        budget: CreditBudget,
        force_fallback: bool,
    ) -> list[_Attempt]:
        attempts: list[_Attempt] = []
        cap = min(budget.remaining, self._settings.firecrawl_max_credits_per_company)
        if not force_fallback:
            if cap >= 2:  # map + homepage at minimum
                primary = await self._attempt(
                    self._primary, ctx, domain, pages, is_fallback=False, max_credits=cap
                )
                budget.spend(primary.record.credits_used)
                attempts.append(primary)
            else:
                logger.warning("skipping primary crawl: run credit budget exhausted")

        needs_fallback = force_fallback or not attempts or not attempts[-1].record.sufficient
        if needs_fallback and self._fallback is not None and self._fallback.status().operational:
            attempts.append(
                await self._attempt(
                    self._fallback, ctx, domain, pages, is_fallback=True, max_credits=None
                )
            )
        return attempts

    @staticmethod
    def _choose(attempts: list[_Attempt]) -> _Attempt | None:
        usable = [a for a in attempts if a.result is not None and a.result.pages]
        if not usable:
            return attempts[-1] if attempts else None
        sufficient = [a for a in usable if a.record.sufficient]
        pool = sufficient or usable
        # prefer the most recent sufficient attempt (fallback runs only if primary fell short)
        return max(
            pool, key=lambda a: (a.record.sufficient, len(a.result.pages) if a.result else 0)
        )
