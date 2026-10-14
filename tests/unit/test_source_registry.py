"""Phase 6 Source Actor Registry: declared != active; disabled / non-permitted sources never run."""

from __future__ import annotations

import pytest

from app.core.errors import SourceNotRunnableError
from app.domain.enums import SourcePolicyState
from app.domain.sources import SOURCES, SourceDefinition, SourceRegistry
from app.providers.apify.mock import MockApifyProvider
from app.providers.contracts import DiscoveryQuery
from app.repositories.discovery import InMemoryDiscoveryStore
from app.services.discovery import DiscoveryService


def test_only_the_reviewed_funding_source_runs_by_default() -> None:
    assert SourceRegistry().runnable() == ["funding_news"]
    assert SourceRegistry.from_setting("funding_news, linkedin ,").enabled == {
        "funding_news",
        "linkedin",
    }


@pytest.mark.parametrize(
    ("source", "why"),
    [
        ("linkedin", "policy state is blocked"),
        ("x_twitter", "policy state is blocked"),
        ("youtube", "policy state is pending_review"),
        ("job_boards", "policy state is pending_review"),
        ("does_not_exist", "unknown source"),
    ],
)
def test_non_permitted_sources_cannot_run_even_when_enabled(source, why) -> None:
    registry = SourceRegistry.from_setting(f"funding_news,{source}")
    with pytest.raises(SourceNotRunnableError, match=why):
        registry.require_runnable(source)


def test_disabled_source_cannot_run() -> None:
    with pytest.raises(SourceNotRunnableError, match="disabled"):
        SourceRegistry.from_setting("").require_runnable("funding_news")


def test_auth_required_or_parserless_sources_never_run() -> None:
    sources = dict(SOURCES)
    sources["cookie_site"] = SourceDefinition(
        "cookie_site", "someone/actor", "needs login", SourcePolicyState.APPROVED, True,
        (), 1, 1, "0.1", "parser",
    )  # fmt: skip
    sources["no_parser"] = SourceDefinition(
        "no_parser", "someone/actor", "no parser yet", SourcePolicyState.APPROVED, False,
        (), 1, 1, "0.1", None,
    )  # fmt: skip
    registry = SourceRegistry(enabled=frozenset({"cookie_site", "no_parser"}), sources=sources)
    assert "authentication" in (registry.blocked_reason("cookie_site") or "")
    assert "parser" in (registry.blocked_reason("no_parser") or "")


def test_every_declared_source_documents_its_policy() -> None:
    for s in SOURCES.values():
        assert s.policy_notes, s.source_id
        if s.policy_state is not SourcePolicyState.APPROVED:
            assert s.max_items_per_run == 0 and s.allowed_fields == ()


async def test_discovery_service_refuses_before_any_call_or_run_row() -> None:
    provider, store = MockApifyProvider(), InMemoryDiscoveryStore()
    with pytest.raises(SourceNotRunnableError):
        await DiscoveryService(provider, store).run(DiscoveryQuery(source="linkedin"))
    assert provider.calls == []
    assert store.runs == {}
    run = await DiscoveryService(provider, store).run(
        DiscoveryQuery(source="funding_news", limit=1)
    )
    assert (run.provider_run_id or "").startswith("mock-run-")
