"""SignalResearchService: VERIFIED-only gate, budgets, policy re-check, dedupe, provenance,
failure recording - plus CLI safety."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar
from uuid import uuid4

import pytest
from pydantic import HttpUrl

from app.core.errors import AppError, ProviderTimeoutError
from app.domain.enums import (
    DomainResolutionStatus,
    ProviderName,
    ResearchRunStatus,
    SignalType,
)
from app.domain.signals import is_forbidden_research_url
from app.providers.agent_reach.mock import MockAgentReachProvider
from app.providers.contracts import (
    EvidenceItem,
    ProviderStatus,
    ResearchQueryLog,
    SignalResearchRequest,
    SignalResearchResult,
)
from app.repositories.signals import InMemorySignalStore, ResearchTarget
from app.services.signal_research import SignalResearchService

ROOT = Path(__file__).resolve().parents[2]


def item(url: str, signal_type: SignalType = SignalType.FUNDING, **kw: Any) -> EvidenceItem:
    base: dict[str, Any] = {
        "signal_type": signal_type,
        "url": HttpUrl(url),
        "snippet": "Drivn raises Rs 45 Cr",
        "title": "Drivn raises Rs 45 Cr",
        "platform": "news:Entrackr",
        "channel": "news_rss",
        "query": '"Drivn"',
        "confidence": 0.7,
    }
    return EvidenceItem(**(base | kw))


class ScriptedResearch:
    name: ClassVar[ProviderName] = ProviderName.AGENT_REACH

    def __init__(self, result: SignalResearchResult | Exception) -> None:
        self.result = result
        self.requests: list[SignalResearchRequest] = []

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            name=self.name, mode="mock", configured=True, operational=True, paid=False
        )

    async def research(self, request: SignalResearchRequest) -> SignalResearchResult:
        self.requests.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def result(
    *items: EvidenceItem, queries: int = 1, processed: int | None = None
) -> SignalResearchResult:
    return SignalResearchResult(
        provider=ProviderName.AGENT_REACH,
        items=list(items),
        queries=[
            ResearchQueryLog(channel="news_rss", query=f"q{i}", status="ok") for i in range(queries)
        ],
        items_processed=len(items) if processed is None else processed,
        channels_used=["news_rss"],
        channels_disabled={"linkedin": "forbidden"},
    )


def verified(**kw: Any) -> ResearchTarget:
    base: dict[str, Any] = {
        "company_id": uuid4(),
        "name": "Drivn",
        "normalized_name": "drivn",
        "domain": "drivn.co.in",
        "domain_status": DomainResolutionStatus.VERIFIED,
        "context_terms": ("leasing",),
    }
    return ResearchTarget(**(base | kw))


def setup(provider, settings, target: ResearchTarget | None = None):
    store = InMemorySignalStore()
    target = target or verified()
    store.add_target(target)
    return SignalResearchService(provider, store, settings), store, target


# ---------------------------------------------------------------- gate
@pytest.mark.parametrize(
    "target",
    [
        verified(domain_status=DomainResolutionStatus.UNRESOLVED, domain=None),
        verified(domain_status=DomainResolutionStatus.REVIEW),
        verified(domain_status=DomainResolutionStatus.RESOLVED),  # resolved != verified
        verified(domain=None),  # verified status without a domain is not enough
    ],
)
async def test_only_verified_companies_are_researched(settings, target) -> None:
    provider = ScriptedResearch(result(item("https://n.example/a")))
    service, store, target = setup(provider, settings, target)
    run = await service.run(target.company_id)
    assert run.status is ResearchRunStatus.SKIPPED
    assert "not VERIFIED" in (run.error_message or "")
    assert provider.requests == []  # nothing left the process
    assert store.runs[run.id].status is ResearchRunStatus.SKIPPED


# ---------------------------------------------------------------- happy path + provenance
async def test_verified_company_signals_are_persisted_with_provenance(settings) -> None:
    provider = ScriptedResearch(
        result(
            item("https://n.example/a"),
            item(
                "https://n.example/b",
                SignalType.HIRING,
                snippet="Drivn is hiring",
                title="Drivn is hiring",
            ),
        )
    )
    service, store, target = setup(provider, settings)
    run = await service.run(target.company_id)

    assert run.status is ResearchRunStatus.SUCCEEDED
    assert (run.signals_created, run.signals_duplicate) == (2, 0)
    req = provider.requests[0]
    assert (req.company_name, req.domain, req.context_terms) == (
        "Drivn",
        "drivn.co.in",
        ["leasing"],
    )
    signals = store.list_signals(target.company_id)
    assert {s.signal_type for s in signals} == {SignalType.FUNDING, SignalType.HIRING}
    assert all(len(s.source_ids) == 1 and 0 <= s.confidence <= 1 for s in signals)
    assert run.channels_disabled == {"linkedin": "forbidden"}
    assert run.queries[0]["channel"] == "news_rss"


async def test_dedupe_across_runs(settings) -> None:
    provider = ScriptedResearch(result(item("https://n.example/a")))
    service, store, target = setup(provider, settings)
    first = await service.run(target.company_id)
    second = await service.run(target.company_id)
    assert (first.signals_created, second.signals_created, second.signals_duplicate) == (1, 0, 1)
    assert second.status is ResearchRunStatus.SUCCEEDED
    assert len(store.list_signals(target.company_id)) == 1


async def test_empty_research_is_recorded_as_empty(settings) -> None:
    service, _, target = setup(ScriptedResearch(result()), settings)
    run = await service.run(target.company_id)
    assert run.status is ResearchRunStatus.EMPTY


# ---------------------------------------------------------------- budgets
async def test_budget_flags_can_only_lower_ceilings(make_settings) -> None:
    s = make_settings(signal_max_queries_per_company=5, signal_max_items_per_company=20)
    provider = ScriptedResearch(result())
    service, _, target = setup(provider, s)
    await service.run(target.company_id, max_queries=99, max_items=2)
    req = provider.requests[0]
    assert (req.max_queries, req.max_results) == (5, 2)


async def test_provider_exceeding_budget_is_rejected_and_nothing_persisted(settings) -> None:
    over = result(item("https://n.example/a"), queries=6)  # default budget is 5 queries
    service, store, target = setup(ScriptedResearch(over), settings)
    with pytest.raises(AppError, match="exceeded the research budget"):
        await service.run(target.company_id)
    (run,) = store.runs.values()
    assert run.status is ResearchRunStatus.FAILED and run.error_code == "budget_violation"
    assert store.list_signals(target.company_id) == []


# ---------------------------------------------------------------- policy (defence in depth)
async def test_forbidden_platforms_and_unknown_channels_are_filtered(settings) -> None:
    provider = ScriptedResearch(
        result(
            item("https://www.linkedin.com/posts/x"),
            item("https://instagram.com/p/y"),
            item("https://n.example/ok"),
            item("https://n.example/other", channel="reddit"),
        )
    )
    service, store, target = setup(provider, settings)
    run = await service.run(target.company_id)
    assert run.signals_created == 1
    assert run.items_dropped == 3
    assert [str(s.summary) for s in store.list_signals(target.company_id)] == [
        "Drivn raises Rs 45 Cr"
    ]


@pytest.mark.parametrize(
    ("url", "forbidden"),
    [
        ("https://in.linkedin.com/company/drivn", True),
        ("https://www.instagram.com/drivn", True),
        ("https://x.com/drivn", True),
        ("https://old.reddit.com/r/ev", True),
        ("https://www.youtube.com/watch?v=1", False),  # public metadata allowed in Phase 4
        ("https://news.google.com/rss/articles/abc", False),
    ],
)
def test_research_host_policy(url: str, forbidden: bool) -> None:
    assert is_forbidden_research_url(url) is forbidden


# ---------------------------------------------------------------- failures
async def test_provider_failure_is_recorded_then_reraised(settings) -> None:
    service, store, target = setup(
        ScriptedResearch(ProviderTimeoutError("agent_reach", "slow")), settings
    )
    with pytest.raises(ProviderTimeoutError):
        await service.run(target.company_id)
    (run,) = store.runs.values()
    assert (run.status, run.error_code) == (ResearchRunStatus.FAILED, "provider_timeout")


async def test_mock_provider_end_to_end(settings) -> None:
    service, _store, target = setup(MockAgentReachProvider(), settings)
    run = await service.run(target.company_id)
    assert run.signals_created == 3
    assert run.queries_used == 2 and run.items_processed == 3


# ---------------------------------------------------------------- CLI safety
def _cli() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "run_signal_research", ROOT / "scripts" / "run_signal_research.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_cli_dry_run_researches_verified_and_skips_unverified(capsys) -> None:
    assert await _cli().main([]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert '"status": "succeeded"' in out and '"status": "skipped"' in out


async def test_cli_refuses_live_without_switch(capsys, monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    assert await _cli().main(["--live"]) == 2
    assert "PROVIDER_MODE must be 'live'" in capsys.readouterr().out
