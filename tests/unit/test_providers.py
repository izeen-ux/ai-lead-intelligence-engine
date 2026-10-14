"""Provider contract tests: every capability has a mock and a live stub honouring the protocol,
mocks are offline and return valid contracts, live stubs refuse to call out."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Coroutine
from typing import Any
from uuid import uuid4

import pytest

from app.core.errors import (
    ProviderCallBlockedError,
    ProviderChannelUnsupportedError,
    ProviderError,
    ProviderNotConfiguredError,
    ProviderNotImplementedError,
)
from app.domain.enums import (
    EmailVerificationStatus,
    EnrichmentStatus,
    EnrichmentTarget,
    ProviderName,
)
from app.domain.qualification.bundle import LAYA_QUESTIONS
from app.providers.base import (
    DiscoveryProvider,
    EmailVerifier,
    EnrichmentOrchestrator,
    EnrichmentVendor,
    LiveAdapterBase,
    LlmReviewProvider,
    Provider,
    QualificationProvider,
    SignalResearchProvider,
    WebsiteIntelligenceProvider,
)
from app.providers.contracts import (
    DiscoveryQuery,
    EnrichmentRequest,
    LlmReviewRequest,
    QualificationRequest,
    SignalResearchRequest,
    WebsiteExtractionRequest,
)
from app.providers.registry import ProviderRegistry

# The deprecated Techno-AI-Router is registered only with REASONING_PROVIDER=ai_router.
# one reasoning provider is active at a time: OpenAI by default (Claude / router are opt-in)
ALL_PROVIDERS = set(ProviderName) - {ProviderName.AI_ROUTER, ProviderName.ANTHROPIC}
# Providers with real live adapters so far (Phase 2: Apify; Phase 3: Firecrawl, Crawl4AI).
IMPLEMENTED = {
    ProviderName.APIFY,
    ProviderName.FIRECRAWL,
    ProviderName.CRAWL4AI,
    ProviderName.AGENT_REACH,  # Phase 4
    ProviderName.LAYA,  # Phase 5 (local sidecar)
    ProviderName.ENRICHMENT_KIT,  # Phase 6 (local enrichment sidecar)
    ProviderName.HUNTER,  # Phase 6 (via the sidecar)
    ProviderName.APOLLO,  # Phase 6 (via the sidecar)
    ProviderName.AI_ROUTER,  # Phase 7 (Techno-AI-Router; deprecated, opt-in)
    ProviderName.OPENAI,  # Phase 10 (active reasoning provider)
}
# Live-refusal tests select NO research channels so Agent-Reach can never reach the network.
NO_CHANNELS = {"agent_reach_channels": ""}


# ---------------------------------------------------------------- registry
def test_mock_registry_covers_all_eight_providers(registry: ProviderRegistry) -> None:
    providers = registry.all()
    assert set(providers) == ALL_PROVIDERS
    for p in providers.values():
        assert isinstance(p, Provider)
        s = p.status()
        assert (s.mode, s.operational, s.paid) == ("mock", True, False)


def test_registry_wires_capabilities_to_intended_vendors(registry: ProviderRegistry) -> None:
    assert isinstance(registry.discovery, DiscoveryProvider)
    assert isinstance(registry.website, WebsiteIntelligenceProvider)
    assert isinstance(registry.website_fallback, WebsiteIntelligenceProvider)
    assert isinstance(registry.signal_research, SignalResearchProvider)
    assert isinstance(registry.qualification, QualificationProvider)
    assert isinstance(registry.enrichment, EnrichmentOrchestrator)
    assert isinstance(registry.email_vendor, EnrichmentVendor)
    assert isinstance(registry.email_verifier, EmailVerifier)
    assert isinstance(registry.people_vendor, EnrichmentVendor)
    assert isinstance(registry.llm_review, LlmReviewProvider)
    assert registry.discovery.name is ProviderName.APIFY
    assert registry.website.name is ProviderName.FIRECRAWL
    assert registry.website_fallback.name is ProviderName.CRAWL4AI
    assert registry.signal_research.name is ProviderName.AGENT_REACH
    assert registry.qualification.name is ProviderName.LAYA
    assert registry.enrichment.name is ProviderName.ENRICHMENT_KIT
    assert registry.email_vendor.name is ProviderName.HUNTER
    assert registry.people_vendor.name is ProviderName.APOLLO


def test_live_registry_uses_non_operational_stubs(make_settings) -> None:
    registry = ProviderRegistry(make_settings(provider_mode="live"))
    for name, p in registry.all().items():
        assert isinstance(p, LiveAdapterBase)
        status = p.status()
        assert status.mode == "live"
        if name in IMPLEMENTED:
            continue  # real adapters; see test_apify_adapter.py / test_website_adapters.py
        assert status.operational is False
        assert "Phase" in (status.detail or "")
    statuses = {s.name: s for s in registry.statuses()}
    assert statuses[ProviderName.HUNTER].paid is True
    assert statuses[ProviderName.HUNTER].configured is False
    assert statuses[ProviderName.CRAWL4AI].paid is False
    assert statuses[ProviderName.CRAWL4AI].configured is False  # needs CRAWL4AI_BASE_URL


def test_apify_is_operational_only_when_configured(make_settings) -> None:
    unconfigured = ProviderRegistry(make_settings(provider_mode="live")).discovery.status()
    assert unconfigured.operational is False  # no token
    configured = ProviderRegistry(
        make_settings(provider_mode="live", apify_token="test-token-000")
    ).discovery.status()
    assert configured.operational is True
    assert configured.paid is True


# ---------------------------------------------------------------- mocks return valid contracts
async def test_mock_discovery(registry: ProviderRegistry) -> None:
    result = await registry.discovery.discover(DiscoveryQuery(source="funding_news", limit=2))
    assert result.provider is ProviderName.APIFY
    assert len(result.candidates) == 2
    assert result.candidates[0].domain == "acme.example"
    assert result.run_ref


async def test_mock_website_primary_and_fallback(registry: ProviderRegistry) -> None:
    ok = await registry.website.extract_site(WebsiteExtractionRequest(domain="acme.example"))
    assert ok.sufficient and ok.pages and ok.provider is ProviderName.FIRECRAWL

    req = WebsiteExtractionRequest(domain="js-heavy.example", page_budget=2)
    weak = await registry.website.extract_site(req)
    assert weak.sufficient is False
    fallback = await registry.website_fallback.extract_site(req)
    assert fallback.sufficient and fallback.provider is ProviderName.CRAWL4AI
    assert 1 <= len(fallback.pages) <= 2  # page budget respected


async def test_mock_signal_research(registry: ProviderRegistry) -> None:
    result = await registry.signal_research.research(
        SignalResearchRequest(company_name="Acme Robotics", founder_name="Asha")
    )
    assert result.items
    assert all(0 <= i.confidence <= 1 and i.url for i in result.items)


async def test_mock_qualification_answers_every_question(registry) -> None:
    result = await registry.qualification.evaluate(
        QualificationRequest(
            subject_id=uuid4(), state={"events": []}, questions=list(LAYA_QUESTIONS)
        )
    )
    assert [o.dimension for o in result.outputs] == [q.dimension for q in LAYA_QUESTIONS]
    assert all(o.confidence is not None and 0 <= o.confidence <= 1 for o in result.outputs)


async def test_mock_llm_review(registry) -> None:
    result = await registry.llm_review.review(
        LlmReviewRequest(subject_id=uuid4(), bundle={}, allowed_evidence_ids=["e1"],
                         qualified_allowed=False)
    )  # fmt: skip
    assert result.provider is ProviderName.OPENAI
    assert result.supporting_evidence_ids == ["e1"]


async def test_mock_enrichment_orchestrator(registry: ProviderRegistry) -> None:
    hit = await registry.enrichment.enrich(
        EnrichmentRequest(lead_id=uuid4(), target=EnrichmentTarget.PERSON, domain="acme.example")
    )
    assert hit.final is not None
    assert hit.final.orchestrated_by is ProviderName.ENRICHMENT_KIT
    assert hit.attempts

    miss = await registry.enrichment.enrich(
        EnrichmentRequest(lead_id=uuid4(), target=EnrichmentTarget.PERSON)
    )
    assert miss.final is None
    assert miss.attempts[0].status is EnrichmentStatus.NOT_FOUND


async def test_mock_hunter_discovery_is_not_verification(registry: ProviderRegistry) -> None:
    found = await registry.email_vendor.enrich(
        EnrichmentRequest(lead_id=uuid4(), target=EnrichmentTarget.PERSON, domain="acme.example")
    )
    assert found.email == "founder@acme.example"
    assert found.email_verification is EmailVerificationStatus.UNVERIFIED
    verified = await registry.email_verifier.verify_email("founder@acme.example")
    assert verified.status is EmailVerificationStatus.VALID


async def test_mock_apollo(registry: ProviderRegistry) -> None:
    r = await registry.people_vendor.enrich(
        EnrichmentRequest(lead_id=uuid4(), target=EnrichmentTarget.COMPANY, domain="acme.example")
    )
    assert r.provider is ProviderName.APOLLO and r.fields


# ---------------------------------------------------------------- live stubs refuse to call out
def _live_calls(
    registry: ProviderRegistry,
) -> dict[ProviderName, Callable[[], Coroutine[Any, Any, object]]]:
    lead = uuid4()
    person = EnrichmentRequest(lead_id=lead, target=EnrichmentTarget.PERSON, domain="acme.io")
    return {
        ProviderName.APIFY: lambda: registry.discovery.discover(DiscoveryQuery(source="x")),
        ProviderName.FIRECRAWL: lambda: registry.website.extract_site(
            WebsiteExtractionRequest(domain="acme.io")
        ),
        ProviderName.CRAWL4AI: lambda: registry.website_fallback.extract_site(
            WebsiteExtractionRequest(domain="acme.io")
        ),
        ProviderName.AGENT_REACH: lambda: registry.signal_research.research(
            SignalResearchRequest(company_name="Acme")
        ),
        ProviderName.LAYA: lambda: registry.qualification.evaluate(
            QualificationRequest(subject_id=lead, state={}, questions=list(LAYA_QUESTIONS))
        ),
        ProviderName.OPENAI: lambda: registry.llm_review.review(
            LlmReviewRequest(subject_id=lead, bundle={}, qualified_allowed=False)
        ),
        ProviderName.ENRICHMENT_KIT: lambda: registry.enrichment.enrich(person),
        ProviderName.HUNTER: lambda: registry.email_vendor.enrich(person),
        ProviderName.APOLLO: lambda: registry.people_vendor.enrich(person),
    }


@pytest.mark.parametrize("provider", sorted(ALL_PROVIDERS))
async def test_paid_guard_blocks_before_anything_else(make_settings, provider) -> None:
    registry = ProviderRegistry(make_settings(provider_mode="live", **NO_CHANNELS))
    call = _live_calls(registry)[provider]
    expected: type[ProviderError] = (
        ProviderCallBlockedError
        if registry.all()[provider].status().paid
        else ProviderNotConfiguredError
        if provider in (ProviderName.CRAWL4AI, ProviderName.LAYA)  # need a local server URL
        else ProviderChannelUnsupportedError
        if provider is ProviderName.AGENT_REACH  # no permitted channel selected
        else ProviderNotImplementedError  # keyless/free providers go straight to the stub
    )
    with pytest.raises(expected) as exc_info:
        await call()
    assert exc_info.value.details["provider"] == provider.value


async def test_missing_credentials_detected_when_paid_calls_allowed(make_settings) -> None:
    registry = ProviderRegistry(make_settings(provider_mode="live", allow_paid_provider_calls=True))
    with pytest.raises(ProviderNotConfiguredError):
        await _live_calls(registry)[ProviderName.HUNTER]()


async def test_ai_review_is_blocked_without_the_paid_switch(make_settings) -> None:
    """The AI review is a paid call: live mode + key is not enough (no request is made)."""
    registry = ProviderRegistry(make_settings(provider_mode="live", openai_api_key="fake-key-000"))
    with pytest.raises(ProviderError):
        await registry.llm_review.review(
            LlmReviewRequest(subject_id=uuid4(), bundle={}, qualified_allowed=False)
        )


async def test_sidecar_vendors_need_the_local_sidecar_url(make_settings) -> None:
    """Phase 6: keys present + paid switch on, but no ENRICHMENT_KIT_BASE_URL -> refused."""
    registry = ProviderRegistry(
        make_settings(
            provider_mode="live", allow_paid_provider_calls=True, hunter_api_key="fake-key-000"
        )
    )
    with pytest.raises(ProviderNotConfiguredError, match="ENRICHMENT_KIT_BASE_URL"):
        await registry.email_verifier.verify_email("a@acme.io")


async def test_no_external_network_attempted_by_any_call(make_settings, network_attempts) -> None:
    for mode in ("mock", "live"):
        registry = ProviderRegistry(make_settings(provider_mode=mode, **NO_CHANNELS))
        for call in _live_calls(registry).values():
            with contextlib.suppress(
                ProviderCallBlockedError,
                ProviderNotImplementedError,
                ProviderNotConfiguredError,
                ProviderChannelUnsupportedError,
            ):
                await call()
    assert network_attempts == []


# ---------------------------------------------------------------- reasoning provider selection
def test_openai_is_the_default_reasoning_provider(make_settings) -> None:
    for mode in ("mock", "live"):
        registry = ProviderRegistry(make_settings(provider_mode=mode))
        assert registry.reasoning.name is ProviderName.OPENAI
        assert registry.llm_review.name is ProviderName.OPENAI
        assert ProviderName.AI_ROUTER not in registry.all()


def test_deprecated_router_needs_an_explicit_opt_in(make_settings) -> None:
    for mode in ("mock", "live"):
        registry = ProviderRegistry(
            make_settings(provider_mode=mode, reasoning_provider="ai_router")
        )
        assert registry.reasoning.name is ProviderName.AI_ROUTER
        assert ProviderName.OPENAI not in registry.all()


def test_router_key_is_not_required_by_default(make_settings) -> None:
    missing = make_settings(provider_mode="live").missing_provider_credentials()
    assert ProviderName.AI_ROUTER not in missing and ProviderName.OPENAI in missing
    legacy = make_settings(reasoning_provider="ai_router").missing_provider_credentials()
    assert ProviderName.AI_ROUTER in legacy and ProviderName.OPENAI not in legacy
