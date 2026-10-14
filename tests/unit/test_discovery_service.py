"""DiscoveryService with the in-memory store: persistence, dedupe, provenance, failures."""

from __future__ import annotations

import asyncio
import importlib.util
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import ClassVar

import pytest
from pydantic import HttpUrl

from app.core.errors import ProviderCallBlockedError, ProviderRunFailedError
from app.domain.enums import ProviderName, RunStatus, SourceType
from app.domain.identity import company_dedupe_key
from app.providers.apify.mock import MockApifyProvider
from app.providers.contracts import CandidateRecord, DiscoveryQuery, DiscoveryResult, ProviderStatus
from app.repositories.discovery import InMemoryDiscoveryStore
from app.services.discovery import DiscoveryService

ROOT = Path(__file__).resolve().parents[2]


def cand(name: str, url: str, domain: str | None = None) -> CandidateRecord:
    return CandidateRecord(
        company_name=name,
        domain=domain,
        source_url=HttpUrl(url),
        source_type=SourceType.NEWS,
        source_title=f"{name} raises funding",
    )


class ScriptedProvider:
    """Returns pre-set results (or raises) in order - one per discover() call."""

    name: ClassVar[ProviderName] = ProviderName.APIFY

    def __init__(self, *outcomes: DiscoveryResult | Exception) -> None:
        self._outcomes = list(outcomes)

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            name=self.name, mode="mock", configured=True, operational=True, paid=False
        )

    async def discover(self, query: DiscoveryQuery, *, on_run_started=None) -> DiscoveryResult:
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def result(*candidates: CandidateRecord, run_ref: str = "run-1") -> DiscoveryResult:
    return DiscoveryResult(
        provider=ProviderName.APIFY,
        actor_id="apify/google-search-scraper",
        run_ref=run_ref,
        provider_status="SUCCEEDED",
        provider_input={"queries": "q"},
        max_items=1,
        max_charge_usd=Decimal("0.02"),
        raw_item_count=1,
        skipped_count=2,
        candidates=list(candidates),
        cost_usd=Decimal("0.0055"),
        charged_events={"search-page-scraped": 1},
    )


QUERY = DiscoveryQuery(source="funding_news", limit=5)


async def test_successful_run_is_recorded_with_counts_and_cost() -> None:
    store = InMemoryDiscoveryStore()
    provider = ScriptedProvider(
        result(
            cand("Acme Robotics", "https://news.example/a"),
            cand("Globex", "https://news.example/b"),
        )
    )
    run = await DiscoveryService(provider, store).run(QUERY)

    assert run.status is RunStatus.SUCCEEDED
    assert run.provider_run_id == "run-1"
    assert run.actor_id == "apify/google-search-scraper"
    assert (run.candidate_count, run.new_company_count, run.duplicate_count) == (2, 2, 0)
    assert run.skipped_count == 2
    assert run.cost_usd == Decimal("0.0055")
    assert run.charged_events == {"search-page-scraped": 1}
    assert run.started_at is not None and run.finished_at is not None
    assert store.get_run(run.id) == run  # persisted final state
    assert store.count_companies() == 2


async def test_dedupe_across_runs_keeps_provenance() -> None:
    store = InMemoryDiscoveryStore()
    provider = ScriptedProvider(
        result(cand("Acme Robotics", "https://news.example/a"), run_ref="run-1"),
        result(
            cand("ACME Robotics Pvt. Ltd.", "https://other.example/x"),  # same company, new source
            cand("Acme Robotics", "https://news.example/a?utm_source=g"),  # same company+source
            run_ref="run-2",
        ),
    )
    service = DiscoveryService(provider, store)
    first = await service.run(QUERY)
    second = await service.run(QUERY)

    assert first.new_company_count == 1
    assert (second.new_company_count, second.duplicate_count) == (0, 2)
    assert store.count_companies() == 1
    key = company_dedupe_key("Acme Robotics")
    company = store.find_company_by_key(key)
    assert company is not None
    assert len(company.source_ids) == 2  # both distinct sources retained
    assert store.times_seen(key) == 3


async def test_in_batch_duplicates_are_counted() -> None:
    store = InMemoryDiscoveryStore()
    provider = ScriptedProvider(
        result(cand("Zeta", "https://a.example/1"), cand("zeta", "https://b.example/2"))
    )
    run = await DiscoveryService(provider, store).run(QUERY)
    assert (run.new_company_count, run.duplicate_count) == (1, 1)


async def test_domain_and_name_keys_are_separate_until_merged_later() -> None:
    store = InMemoryDiscoveryStore()
    provider = ScriptedProvider(
        result(cand("Acme", "https://n.example/1"), cand("Acme", "https://n.example/2", "acme.io"))
    )
    await DiscoveryService(provider, store).run(QUERY)
    assert store.count_companies() == 2  # documented limitation (ADR-021)


async def test_provider_failure_is_recorded_then_reraised() -> None:
    store = InMemoryDiscoveryStore()
    failure = ProviderRunFailedError(
        "apify", "run failed", details={"run_id": "run-9", "status": "FAILED", "cost_usd": "0.001"}
    )
    with pytest.raises(ProviderRunFailedError):
        await DiscoveryService(ScriptedProvider(failure), store).run(QUERY)
    (run,) = store.runs.values()
    assert run.status is RunStatus.FAILED
    assert run.error_code == "provider_run_failed"
    assert run.provider_run_id == "run-9"
    assert run.provider_status == "FAILED"
    assert run.cost_usd == Decimal("0.001")
    assert store.count_companies() == 0


async def test_blocked_call_is_recorded_as_failed_run() -> None:
    store = InMemoryDiscoveryStore()
    with pytest.raises(ProviderCallBlockedError):
        await DiscoveryService(
            ScriptedProvider(ProviderCallBlockedError("apify", "blocked")), store
        ).run(QUERY)
    (run,) = store.runs.values()
    assert run.error_code == "provider_call_blocked"
    assert run.cost_usd is None


async def test_unexpected_exception_is_recorded_as_internal_error() -> None:
    store = InMemoryDiscoveryStore()
    with pytest.raises(RuntimeError):
        await DiscoveryService(ScriptedProvider(RuntimeError("boom")), store).run(QUERY)
    (run,) = store.runs.values()
    assert (run.status, run.error_code, run.error_message) == (
        RunStatus.FAILED,
        "internal_error",
        "RuntimeError",
    )


async def test_mock_provider_end_to_end() -> None:
    store = InMemoryDiscoveryStore()
    run = await DiscoveryService(MockApifyProvider(), store).run(QUERY)
    assert run.status is RunStatus.SUCCEEDED
    assert store.count_companies() == 3


# ---------------------------------------------------------------- discovery request validation
@pytest.mark.parametrize(
    "kwargs",
    [
        {"source": ""},
        {"source": "x", "limit": 0},
        {"source": "x", "max_items": 0},
        {"source": "x", "max_charge_usd": Decimal(0)},
        {"source": "x", "country_codes": ["IND"]},
        {"source": "x", "keywords": ["k" * 301]},
        {"source": "x", "keywords": ["k"] * 11},
    ],
)
def test_invalid_discovery_requests_are_rejected(kwargs) -> None:
    with pytest.raises(ValueError, match="validation error"):
        DiscoveryQuery(**kwargs)


def test_discovery_request_normalizes_input() -> None:
    q = DiscoveryQuery(source="x", keywords=[" a ", " "], country_codes=[" in "])
    assert (q.keywords, q.country_codes) == (["a"], ["IN"])


# ---------------------------------------------------------------- CLI safety
def _load_cli() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "run_discovery", ROOT / "scripts" / "run_discovery.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_cli_dry_run_uses_mock_and_memory(capsys) -> None:
    code = await _load_cli().main(["--limit", "2"])
    out = capsys.readouterr().out
    assert code == 0
    assert "DRY RUN" in out
    assert '"status": "succeeded"' in out


async def test_cli_refuses_live_without_switches(capsys, monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)  # no .env here -> defaults (mock, paid calls blocked)
    code = await _load_cli().main(["--live"])
    out = capsys.readouterr().out
    assert code == 2
    assert "REFUSING live run" in out
    assert "PROVIDER_MODE must be 'live'" in out
    assert "ALLOW_PAID_PROVIDER_CALLS must be true" in out


# ---------------------------------------------------------------- D1: run id early, cancellation
class StartedThenInterrupted:
    """Reports a vendor run id, then the wait is interrupted (Ctrl+C / task cancelled)."""

    name: ClassVar[ProviderName] = ProviderName.APIFY

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.seen_run_status: RunStatus | None = None

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            name=self.name, mode="mock", configured=True, operational=True, paid=False
        )

    async def discover(self, query: DiscoveryQuery, *, on_run_started=None) -> DiscoveryResult:
        assert on_run_started is not None
        await on_run_started("apify-run-42")
        raise self.exc


@pytest.mark.parametrize("exc", [asyncio.CancelledError(), KeyboardInterrupt()])
async def test_interrupted_run_is_cancelled_not_left_running(exc: BaseException) -> None:
    store = InMemoryDiscoveryStore()
    with pytest.raises(type(exc)):
        await DiscoveryService(StartedThenInterrupted(exc), store).run(QUERY)
    [saved] = store.runs.values()
    assert saved.status is RunStatus.CANCELLED
    assert saved.provider_run_id == "apify-run-42"  # kept for reconciliation
    assert saved.error_code == "cancelled"
    assert saved.finished_at is not None


async def test_run_id_is_persisted_before_the_provider_returns() -> None:
    store = InMemoryDiscoveryStore()
    snapshots: list[str | None] = []

    class Peek(StartedThenInterrupted):
        async def discover(self, query, *, on_run_started=None):
            await on_run_started("apify-run-7")
            snapshots.extend(r.provider_run_id for r in store.runs.values())
            return result(run_ref="apify-run-7")

    run = await DiscoveryService(Peek(RuntimeError()), store).run(QUERY)
    assert snapshots == ["apify-run-7"]
    assert run.status is RunStatus.SUCCEEDED


async def test_mock_provider_reports_run_id_through_hook() -> None:
    seen: list[str] = []

    async def hook(run_id: str) -> None:
        seen.append(run_id)

    await MockApifyProvider().discover(QUERY, on_run_started=hook)
    assert len(seen) == 1 and seen[0].startswith("mock-run-")
