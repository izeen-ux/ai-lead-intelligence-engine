"""Discovery orchestration (Phase 2).

``DiscoveryService.run(query)``:
  1. records a RUNNING ``DiscoveryRun`` *before* calling the provider (so even a crash leaves an
     audit row);
  2. calls the capability ``DiscoveryProvider`` (Apify live adapter or mock) - never a vendor
     class directly;
  3. persists candidates through ``DiscoveryStore`` (dedupe + provenance, one transaction);
  4. finalizes the run with counts, provider run id, cost and status - or FAILED with the error;
     the provider run id is saved as soon as the vendor job starts, and an interrupt/cancel marks
     the run CANCELLED instead of leaving it RUNNING (Phase 5 fix D1).

Discovery never enriches or contacts anyone: the output is the candidate pool only.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation

from app.core.errors import AppError
from app.core.logging import get_logger
from app.domain.enums import RunStatus
from app.domain.models import DiscoveryRun, utcnow
from app.domain.sources import SourceRegistry
from app.providers.base import DiscoveryProvider
from app.providers.contracts import DiscoveryQuery
from app.repositories.discovery import DiscoveryStore

logger = get_logger(__name__)


def _cost_from_details(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


class DiscoveryService:
    def __init__(
        self,
        provider: DiscoveryProvider,
        store: DiscoveryStore,
        sources: SourceRegistry | None = None,
        *,
        persist_lock: asyncio.Lock | None = None,
    ) -> None:
        self._provider = provider
        self._store = store
        # Sources may be searched at the same time; their results are still stored one source
        # after the other, so two sources naming the same company never race on it.
        self._persist_lock = persist_lock or asyncio.Lock()
        # Source Actor Registry (Phase 6): unknown / disabled / non-approved sources never run.
        self._sources = sources or SourceRegistry()

    async def run(self, query: DiscoveryQuery) -> DiscoveryRun:
        self._sources.require_runnable(query.source)  # before any run row or provider call
        run = DiscoveryRun(
            provider=self._provider.name,
            status=RunStatus.RUNNING,
            query=query.model_dump(mode="json"),
            max_items=query.max_items,
            max_charge_usd=query.max_charge_usd,
            started_at=utcnow(),
        )
        await asyncio.to_thread(self._store.create_run, run)
        logger.info("discovery run started", extra={"run_id": str(run.id), "source": query.source})

        async def record_run_id(provider_run_id: str) -> None:
            # persisted before the provider blocks on the job (Phase 5 fix D1)
            run.provider_run_id = provider_run_id
            await asyncio.to_thread(self._store.save_run, run)

        try:
            result = await self._provider.discover(query, on_run_started=record_run_id)
        except AppError as exc:
            # error fields first: the model re-validates on each assignment and a FAILED run
            # without an error_code is invalid.
            run.error_code = exc.code
            run.error_message = exc.message
            run.status = RunStatus.FAILED
            run.provider_run_id = exc.details.get("run_id") or run.provider_run_id
            run.provider_status = exc.details.get("status")
            run.cost_usd = _cost_from_details(exc.details.get("cost_usd"))
            run.estimated_cost_usd = _cost_from_details(exc.details.get("estimated_cost_usd"))
            run.finished_at = utcnow()
            await asyncio.to_thread(self._store.save_run, run)
            logger.warning(
                "discovery run failed", extra={"run_id": str(run.id), "error_code": exc.code}
            )
            raise
        except Exception as exc:
            run.error_code = "internal_error"
            run.error_message = type(exc).__name__
            run.status = RunStatus.FAILED
            run.finished_at = utcnow()
            await asyncio.to_thread(self._store.save_run, run)
            raise
        except BaseException as exc:
            # Ctrl+C / task cancellation: never leave the row RUNNING. Saved synchronously
            # because the event loop may be shutting down. The provider job may still finish
            # (and bill) on the vendor side - provider_run_id allows reconciliation.
            run.error_code = "cancelled"
            run.error_message = (
                f"{type(exc).__name__} while waiting for the provider; results not persisted"
            )
            run.status = RunStatus.CANCELLED
            run.finished_at = utcnow()
            self._store.save_run(run)
            logger.warning(
                "discovery run cancelled",
                extra={"run_id": str(run.id), "provider_run_id": run.provider_run_id},
            )
            raise

        async with self._persist_lock:
            outcomes = await asyncio.to_thread(
                self._store.persist_candidates,
                run.id,
                result.provider,
                result.run_ref,
                result.candidates,
            )
        new_companies = sum(1 for o in outcomes if o.new_company)
        run.actor_id = result.actor_id
        run.provider_run_id = result.run_ref
        run.provider_status = result.provider_status
        run.provider_input = result.provider_input
        run.max_items = result.max_items or run.max_items
        run.max_charge_usd = result.max_charge_usd or run.max_charge_usd
        run.raw_item_count = result.raw_item_count
        run.candidate_count = len(result.candidates)
        run.new_company_count = new_companies
        run.duplicate_count = len(outcomes) - new_companies
        run.skipped_count = result.skipped_count
        run.estimated_cost_usd = result.estimated_cost_usd
        run.cost_usd = result.cost_usd
        run.charged_events = result.charged_events
        run.status = RunStatus.SUCCEEDED
        run.finished_at = utcnow()
        await asyncio.to_thread(self._store.save_run, run)
        logger.info(
            "discovery run succeeded",
            extra={
                "run_id": str(run.id),
                "provider_run_id": run.provider_run_id,
                "candidates": run.candidate_count,
                "new_companies": run.new_company_count,
                "duplicates": run.duplicate_count,
                "skipped": run.skipped_count,
                "cost_usd": str(run.cost_usd) if run.cost_usd is not None else None,
            },
        )
        return run
