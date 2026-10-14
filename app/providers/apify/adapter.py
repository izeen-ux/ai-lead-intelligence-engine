"""Live Apify adapter (Phase 2) built on the official ``apify-client`` (v3, async).

Flow for ``discover``:
  1. safety guard (paid-call switch, credentials) - inherited from ``LiveAdapterBase``;
  2. resolve the Actor spec and clamp spend controls to the configured ceilings;
  3. run the Actor with ``max_items`` + ``max_total_charge_usd`` + ``run_timeout``;
  4. read at most ``max_items`` dataset items, normalize them via the spec;
  5. return a ``DiscoveryResult`` carrying run id, status, timestamps, cost and charged events.

The token is passed to the client only; it never appears in logs, errors or results.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from apify_client import ApifyClientAsync
from apify_client import errors as apify_errors

from app.core.config import Settings
from app.core.errors import (
    DomainValidationError,
    ProviderCallBlockedError,
    ProviderError,
    ProviderNotConfiguredError,
    ProviderRunFailedError,
    ProviderUnavailableError,
)
from app.core.keyring import KeyRing, ring_for
from app.core.logging import get_logger
from app.domain.enums import ProviderName
from app.providers.apify.actors import ActorSpec, NormalizedBatch, actor_specs
from app.providers.apify.social import SocialPost
from app.providers.base import LiveAdapterBase, RunStartedHook
from app.providers.contracts import CandidateRecord, DiscoveryQuery, DiscoveryResult

logger = get_logger(__name__)
PostReaderFn = Callable[
    [list[SocialPost], list[CandidateRecord], DiscoveryQuery], Awaitable[list[CandidateRecord]]
]
WAIT_ATTEMPTS = 3  # waits for one started run before the source is reported unavailable
WAIT_RETRY_SECONDS = 5.0

# Apify rejects ``max_total_charge_usd`` below this (observed as HTTP 400 on 2026-09-28:
# "Maximum cost per run is less than the allowed minimum of $0.50").
APIFY_MIN_CHARGE_CAP_USD = Decimal("0.50")


# Minimal structural types for the parts of apify-client we use (keeps tests client-free).
class _ActorClient(Protocol):
    async def start(
        self,
        *,
        run_input: Any = ...,
        max_items: int | None = ...,
        max_total_charge_usd: Decimal | None = ...,
        run_timeout: timedelta | None = ...,
    ) -> Any: ...


class _DatasetClient(Protocol):
    def iterate_items(self, *, limit: int | None = ...) -> AsyncIterator[dict[str, Any]]: ...


class _RunClient(Protocol):
    async def get(self) -> Any: ...

    async def wait_for_finish(self, *, wait_duration: timedelta | None = ...) -> Any: ...


class ApifyClientLike(Protocol):
    def actor(self, actor_id: str) -> _ActorClient: ...

    def run(self, run_id: str) -> _RunClient: ...

    def dataset(self, dataset_id: str) -> _DatasetClient: ...


ClientFactory = Callable[[str], ApifyClientLike]


def _default_client_factory(token: str) -> ApifyClientLike:
    client: ApifyClientLike = ApifyClientAsync(token=token)
    return client


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


class ApifyAdapter(LiveAdapterBase):
    name = ProviderName.APIFY
    implemented_in_phase = 2
    operational = True

    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: ClientFactory | None = None,
        specs: dict[str, ActorSpec] | None = None,
        post_reader: PostReaderFn | None = None,
    ) -> None:
        super().__init__(settings)
        self._client_factory = client_factory or _default_client_factory
        self._specs = specs if specs is not None else actor_specs()
        # AI reading of social posts the patterns could not read (DISCOVERY_LLM_EXTRACTION)
        self._post_reader = post_reader

    # -------------------------------------------------------------------- controls
    def effective_limits(
        self, query: DiscoveryQuery, spec: ActorSpec | None = None
    ) -> tuple[int, Decimal]:
        """Clamp per-request limits to configured ceilings (requests can only lower them).
        A spec may name its own item ceiling setting (posts / jobs instead of search pages)."""
        s = self._settings
        ceiling = int(getattr(s, getattr(spec, "items_setting", "apify_max_items_per_run")))
        max_items = min(query.max_items or ceiling, ceiling)
        max_charge = min(
            query.max_charge_usd or s.apify_max_charge_usd_per_run, s.apify_max_charge_usd_per_run
        )
        if max_charge < APIFY_MIN_CHARGE_CAP_USD:
            raise DomainValidationError(
                f"Apify rejects max_total_charge_usd below ${APIFY_MIN_CHARGE_CAP_USD}; "
                "use APIFY_MAX_ESTIMATED_COST_USD_PER_RUN for a tighter budget",
                details={"requested": str(max_charge)},
            )
        return max_items, max_charge

    def _check_estimate(self, spec: ActorSpec, run_input: dict[str, Any]) -> Decimal:
        estimate = spec.estimate_cost_usd(run_input)
        budget = self._settings.apify_max_estimated_cost_usd_per_run
        if estimate > budget:
            raise ProviderCallBlockedError(
                self.name.value,
                f"estimated worst-case cost ${estimate} exceeds "
                f"APIFY_MAX_ESTIMATED_COST_USD_PER_RUN=${budget}",
                details={"estimated_cost_usd": str(estimate), "budget_usd": str(budget)},
            )
        return estimate

    def _spec(self, query: DiscoveryQuery) -> ActorSpec:
        actor_id = query.actor_id or self._settings.apify_discovery_actor_id
        spec = self._specs.get(actor_id)
        if spec is None:
            raise DomainValidationError(
                "no Actor spec registered for this Actor (input/output must be verified first)",
                details={"actor_id": actor_id, "supported": sorted(self._specs)},
            )
        return spec

    # -------------------------------------------------------------------- discover
    async def discover(
        self, query: DiscoveryQuery, *, on_run_started: RunStartedHook | None = None
    ) -> DiscoveryResult:
        self._ensure_callable("discover")
        spec = self._spec(query)
        max_items, max_charge = self.effective_limits(query, spec)
        run_input = spec.build_input(query.model_copy(update={"max_items": max_items}))
        estimate = self._check_estimate(spec, run_input)
        run_input = {k: v for k, v in run_input.items() if not k.startswith("_")}
        ring = self._ring()
        key = ring.current()
        if key is None:  # pragma: no cover - _ensure_callable already checked
            raise ProviderNotConfiguredError(self.name.value, "APIFY_TOKEN missing")
        client = self._client_factory(key)
        run_timeout = timedelta(seconds=self._settings.apify_run_timeout_seconds)
        run_id: str | None = None  # known once the Actor run exists (then nothing is retried)

        logger.info(
            "apify run starting",
            extra={
                "actor_id": spec.actor_id,
                "max_items": max_items,
                "max_charge_usd": str(max_charge),
                "estimated_cost_usd": str(estimate),
                "query_source": query.source,
            },
        )
        try:
            # start + wait (not ``call``): the run id is known - and handed to the caller via
            # ``on_run_started`` - before we block, so an interrupted process still leaves the
            # id in the audit row for reconciliation (Phase 5 fix D1).
            started = await client.actor(spec.actor_id).start(
                run_input=run_input,
                max_items=max_items,
                max_total_charge_usd=max_charge,
                run_timeout=run_timeout,
            )
            run_id = str(started.id)
            logger.info(
                "apify run started", extra={"run_id": run_id, "key_position": ring.position(key)}
            )
            if on_run_started is not None:
                await on_run_started(run_id)
            run = await self._wait(client, run_id, run_timeout + timedelta(seconds=60))
            if run is None:
                raise ProviderUnavailableError(
                    self.name.value,
                    "Actor run did not finish within the wait window",
                    details={"run_id": run_id},
                )
            info = self._run_info(run)
            logger.info("apify run finished", extra={k: str(v) for k, v in info.items()})
            failure = ProviderRunFailedError(
                self.name.value,
                f"Apify run ended with status {info['status']}",
                details={
                    "run_id": info["run_id"],
                    "status": info["status"],
                    "cost_usd": str(info["cost_usd"]) if info["cost_usd"] is not None else None,
                },
            )
            # A run that hit its time limit has usually stored (and billed) part of the
            # results: those are used. Every other non-success status is a failure.
            timed_out = info["status"] == "TIMED-OUT"
            if info["status"] != "SUCCEEDED" and not timed_out:
                raise failure
            items = [
                item
                async for item in client.dataset(info["dataset_id"]).iterate_items(limit=max_items)
            ]
            if timed_out:
                if not items:
                    raise failure
                logger.warning(
                    "apify run timed out - using its partial results",
                    extra={"run_id": info["run_id"], "items": len(items)},
                )
            # Pay-per-event charges can settle after the run reports SUCCEEDED (observed live:
            # the page event appeared only on a later read). Re-read once for final usage.
            info = self._refresh_usage(info, await client.run(info["run_id"]).get())
        except ProviderError:
            raise
        except apify_errors.ApifyClientError as exc:
            error = self._map_client_error(exc)
            # This key is rejected or out of credits and no run was started (nothing billed):
            # switch to the next backup key and repeat the same request.
            if run_id is None and self._key_unusable(ring, key, error):
                return await self.discover(query, on_run_started=on_run_started)
            raise error from None
        except Exception as exc:
            # Any transport failure (the client's HTTP library raises its own ConnectError /
            # timeout types, not OSError). Mapped so ONE source fails and the pipeline goes on
            # instead of the whole run crashing (seen live 2026-10-12).
            raise ProviderUnavailableError(
                self.name.value, f"network error talking to Apify: {type(exc).__name__}"
            ) from None

        batch = spec.normalize(items, query)
        posts_of = getattr(spec, "posts", None)
        if self._post_reader is not None and posts_of is not None:
            extra = await self._post_reader(posts_of(items), batch.candidates, query)
            batch = NormalizedBatch(
                candidates=[*batch.candidates, *extra],
                skipped=max(batch.skipped - len({str(c.source_url) for c in extra}), 0),
            )
        return DiscoveryResult(
            provider=self.name,
            actor_id=spec.actor_id,
            run_ref=info["run_id"],
            provider_status=info["status"],
            provider_input=run_input,
            max_items=max_items,
            max_charge_usd=max_charge,
            started_at=info["started_at"],
            finished_at=info["finished_at"],
            raw_item_count=len(items),
            skipped_count=batch.skipped + max(0, len(batch.candidates) - query.limit),
            candidates=batch.candidates[: query.limit],
            estimated_cost_usd=estimate,
            cost_usd=info["cost_usd"],
            charged_events=info["charged_events"],
        )

    # -------------------------------------------------------------------- helpers
    def _ring(self) -> KeyRing:
        return ring_for(self.name.value, self._settings.provider_keys("apify_token"))

    @staticmethod
    def _key_unusable(ring: KeyRing, key: str, error: ProviderError) -> bool:
        """Bench ``key`` when Apify says it is rejected (401/403) or out of credits (402 /
        a usage-limit error). True = another key is available and the call may be repeated."""
        status = error.details.get("http_status")
        kind = str(error.details.get("api_error_type") or "").lower()
        exhausted = status == 402 or "limit" in kind or "usage" in kind
        if not exhausted and status not in (401, 403):
            return False
        return ring.bench(key, rejected=not exhausted)

    @staticmethod
    async def _wait(client: Any, run_id: str, wait: timedelta) -> Any:
        """Wait for the started run. The job keeps running (and billing) on Apify's side, so a
        short network drop while waiting is retried instead of throwing the results away."""
        for attempt in range(WAIT_ATTEMPTS):
            try:
                return await client.run(run_id).wait_for_finish(wait_duration=wait)
            except (ProviderError, apify_errors.ApifyClientError):
                raise
            except Exception as exc:
                if attempt == WAIT_ATTEMPTS - 1:
                    raise
                logger.warning(
                    "apify wait interrupted - retrying",
                    extra={"run_id": run_id, "error": type(exc).__name__},
                )
                await asyncio.sleep(WAIT_RETRY_SECONDS)
        return None

    @staticmethod
    def _run_info(run: Any) -> dict[str, Any]:
        """Read the typed ``Run`` model duck-typed (its class is private in apify-client)."""
        started: datetime | None = getattr(run, "started_at", None)
        finished: datetime | None = getattr(run, "finished_at", None)
        return {
            "run_id": str(run.id),
            "status": str(run.status),
            "dataset_id": str(run.default_dataset_id),
            "started_at": started,
            "finished_at": finished,
            "cost_usd": _decimal_or_none(getattr(run, "usage_total_usd", None)),
            "charged_events": dict(getattr(run, "charged_event_counts", None) or {}),
        }

    @classmethod
    def _refresh_usage(cls, info: dict[str, Any], refreshed: Any) -> dict[str, Any]:
        if refreshed is None:
            return info
        latest = cls._run_info(refreshed)
        merged = dict(info)
        if latest["cost_usd"] is not None and (
            info["cost_usd"] is None or latest["cost_usd"] > info["cost_usd"]
        ):
            merged["cost_usd"] = latest["cost_usd"]
        if latest["charged_events"]:
            merged["charged_events"] = latest["charged_events"]
        return merged

    def _map_client_error(self, exc: apify_errors.ApifyClientError) -> ProviderError:
        kind = type(exc).__name__
        status = getattr(exc, "status_code", None)
        details: dict[str, Any] = {"error_type": kind, "http_status": status}
        api_type = getattr(exc, "type", None)
        api_message = getattr(exc, "message", None)
        if api_type:
            details["api_error_type"] = str(api_type)[:100]
        if api_message:
            # Apify's own explanation (e.g. input validation) - truncated and token-scrubbed.
            text = str(api_message)[:500]
            for secret in self._settings.provider_keys("apify_token"):
                text = text.replace(secret, "***REDACTED***")
            details["api_message"] = text
        if isinstance(exc, apify_errors.UnauthorizedError | apify_errors.ForbiddenError):
            return ProviderNotConfiguredError(
                self.name.value, "Apify rejected the credentials", details=details
            )
        if isinstance(exc, apify_errors.RateLimitError | apify_errors.ServerError):
            return ProviderUnavailableError(
                self.name.value, "Apify temporarily unavailable", details=details
            )
        suffix = f": {details['api_message']}" if "api_message" in details else ""
        return ProviderError(
            self.name.value, f"Apify request failed ({kind}){suffix}", details=details
        )
