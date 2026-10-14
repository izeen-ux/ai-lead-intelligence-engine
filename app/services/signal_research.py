"""Signal research orchestration (Phase 4).

``SignalResearchService.run(company_id)``:
  1. GATE - only VERIFIED companies with a canonical domain are researched; anything else is
     recorded as a SKIPPED run and nothing leaves the process.
  2. BUDGET - per-company query/item limits (requests can only lower the configured ceilings).
  3. RESEARCH - through the vendor-neutral ``SignalResearchProvider`` (Agent-Reach live, mock
     in tests / dry runs).
  4. DEFENCE IN DEPTH - re-check what came back: budget respected, only permitted channels,
     no forbidden-platform URLs (LinkedIn/Instagram/X/...), bounded confidence.
  5. PERSIST - sources + signals with provenance (dedupe across runs), and the run record.

It never changes company identity or verified domains, never calls enrichment providers and
never creates leads/qualifications (Phase 5+).
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from app.core.config import Settings
from app.core.errors import AppError, ProviderError
from app.core.logging import get_logger
from app.domain.enums import ResearchChannel, ResearchRunStatus
from app.domain.models import SignalResearchRunRecord, utcnow
from app.domain.signals import is_forbidden_research_url
from app.providers.base import SignalResearchProvider
from app.providers.contracts import EvidenceItem, SignalResearchRequest
from app.repositories.signals import SignalStore

logger = get_logger(__name__)

_PERMITTED = {c.value for c in ResearchChannel}


class SignalResearchService:
    def __init__(
        self, provider: SignalResearchProvider, store: SignalStore, settings: Settings
    ) -> None:
        self._provider = provider
        self._store = store
        self._settings = settings

    def budgets(self, max_queries: int | None, max_items: int | None) -> tuple[int, int]:
        s = self._settings
        return (
            min(max_queries or s.signal_max_queries_per_company, s.signal_max_queries_per_company),
            min(max_items or s.signal_max_items_per_company, s.signal_max_items_per_company),
        )

    async def run(
        self,
        company_id: UUID,
        *,
        max_queries: int | None = None,
        max_items: int | None = None,
    ) -> SignalResearchRunRecord:
        target = await asyncio.to_thread(self._store.research_target, company_id)
        q_budget, i_budget = self.budgets(max_queries, max_items)
        run = SignalResearchRunRecord(
            company_id=company_id,
            provider=self._provider.name,
            status=ResearchRunStatus.SKIPPED,
            max_queries=q_budget,
            max_items=i_budget,
        )

        if not target.is_verified:
            run.error_message = (
                f"company not VERIFIED (domain_status={target.domain_status.value}); "
                "Phase 4 researches verified companies only"
            )
            run.finished_at = utcnow()
            await asyncio.to_thread(self._store.create_run, run)
            logger.info("research skipped: not verified", extra={"company_id": str(company_id)})
            return run

        request = SignalResearchRequest(
            company_name=target.name,
            normalized_name=target.normalized_name,
            domain=target.domain,
            context_terms=list(target.context_terms),
            max_queries=q_budget,
            max_results=i_budget,
        )
        logger.info(
            "research started",
            extra={"company_id": str(company_id), "max_queries": q_budget, "max_items": i_budget},
        )
        try:
            result = await self._provider.research(request)
        except ProviderError as exc:
            run.error_code, run.error_message = exc.code, exc.message
            run.status = ResearchRunStatus.FAILED
            run.finished_at = utcnow()
            await asyncio.to_thread(self._store.create_run, run)
            logger.warning("research failed", extra={"error_code": exc.code})
            raise

        if len(result.queries) > q_budget or result.items_processed > i_budget:
            run.error_code = "budget_violation"
            run.error_message = "provider exceeded the research budget; nothing persisted"
            run.status = ResearchRunStatus.FAILED
            run.finished_at = utcnow()
            await asyncio.to_thread(self._store.create_run, run)
            raise AppError(run.error_message, details={"company_id": str(company_id)})

        accepted, rejected = self._policy_filter(result.items)
        run.channels_used = result.channels_used
        run.channels_disabled = result.channels_disabled
        run.queries = [q.model_dump() for q in result.queries]
        run.queries_used = len(result.queries)
        run.items_processed = result.items_processed
        run.items_dropped = result.items_dropped + rejected
        await asyncio.to_thread(self._store.create_run, run)

        counts = await asyncio.to_thread(self._store.persist_signals, run.id, company_id, accepted)
        run.signals_created, run.signals_duplicate = counts.created, counts.duplicate
        run.status = (
            ResearchRunStatus.SUCCEEDED
            if counts.created + counts.duplicate
            else ResearchRunStatus.EMPTY
        )
        run.finished_at = utcnow()
        await asyncio.to_thread(self._store.save_run, run)
        logger.info(
            "research finished",
            extra={
                "company_id": str(company_id),
                "status": run.status.value,
                "queries": run.queries_used,
                "processed": run.items_processed,
                "signals_created": run.signals_created,
                "signals_duplicate": run.signals_duplicate,
            },
        )
        return run

    @staticmethod
    def _policy_filter(items: list[EvidenceItem]) -> tuple[list[EvidenceItem], int]:
        accepted: list[EvidenceItem] = []
        rejected = 0
        for item in items:
            if item.channel not in _PERMITTED or is_forbidden_research_url(str(item.url)):
                rejected += 1
                continue
            accepted.append(item)
        return accepted, rejected
