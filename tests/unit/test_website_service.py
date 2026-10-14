"""WebsiteIntelligenceService with scripted providers + in-memory store."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import ClassVar
from uuid import uuid4

import pytest
from pydantic import HttpUrl

from app.core.errors import ProviderUnavailableError
from app.domain.enums import CrawlStatus, DomainResolutionStatus, PageType, ProviderName
from app.providers.contracts import (
    ProviderStatus,
    WebPage,
    WebSearchHit,
    WebSearchQuery,
    WebSearchResult,
    WebsiteExtractionRequest,
    WebsiteExtractionResult,
)
from app.repositories.website import CompanyContext, InMemoryWebsiteStore
from app.services.website_intelligence import CreditBudget, WebsiteIntelligenceService

ROOT = Path(__file__).resolve().parents[2]
TEXT = "Drivn electric mobility platform for fleets. " * 20


class ScriptedSearch:
    name: ClassVar[ProviderName] = ProviderName.FIRECRAWL

    def __init__(self, hits: list[WebSearchHit] | Exception) -> None:
        self.hits = hits
        self.queries: list[WebSearchQuery] = []

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            name=self.name, mode="mock", configured=True, operational=True, paid=True
        )

    async def search_web(self, query: WebSearchQuery) -> WebSearchResult:
        self.queries.append(query)
        if isinstance(self.hits, Exception):
            raise self.hits
        return WebSearchResult(
            provider=self.name, query=query.query, hits=self.hits, credits_used=2
        )


class ScriptedCrawler:
    def __init__(
        self,
        name: ProviderName,
        *,
        title: str = "Drivn - Home",
        sufficient: bool = True,
        error: Exception | None = None,
        credits: int = 4,
        operational: bool = True,
    ) -> None:
        self.name = name
        self.title, self.sufficient, self.error = title, sufficient, error
        self.credits, self.operational = credits, operational
        self.requests: list[WebsiteExtractionRequest] = []

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            name=self.name, mode="mock", configured=True, operational=self.operational, paid=False
        )

    async def extract_site(self, request: WebsiteExtractionRequest) -> WebsiteExtractionResult:
        self.requests.append(request)
        if self.error:
            raise self.error
        pages = (
            [
                WebPage(
                    url=HttpUrl(f"https://{request.domain}/"),
                    page_type=PageType.HOME,
                    markdown=TEXT,
                    title=self.title,
                )
            ]
            if self.sufficient
            else []
        )
        return WebsiteExtractionResult(
            provider=self.name,
            domain=request.domain,
            pages=pages,
            sufficient=self.sufficient,
            reason=None if self.sufficient else "homepage missing",
            pages_attempted=1,
            credits_used=self.credits,
        )


OFFICIAL = [
    WebSearchHit(
        url="https://www.linkedin.com/company/drivn", title="Drivn | LinkedIn", position=1
    ),
    WebSearchHit(url="https://drivn.in/", title="Drivn - EV fleets", position=2),
]


def setup(
    search: ScriptedSearch | None = None,
    primary: ScriptedCrawler | None = None,
    fallback: ScriptedCrawler | None = None,
    *,
    settings,
    name: str = "Drivn",
) -> tuple[
    WebsiteIntelligenceService,
    InMemoryWebsiteStore,
    CompanyContext,
    ScriptedCrawler,
    ScriptedCrawler,
]:
    store = InMemoryWebsiteStore()
    ctx = CompanyContext(
        company_id=uuid4(),
        name=name,
        normalized_name=name.lower(),
        domain=None,
        domain_status=DomainResolutionStatus.UNRESOLVED,
        publisher_hosts=frozenset({"news.example"}),
    )
    store.add_company(ctx)
    primary = primary or ScriptedCrawler(ProviderName.FIRECRAWL)
    fallback = fallback or ScriptedCrawler(ProviderName.CRAWL4AI, credits=0)
    service = WebsiteIntelligenceService(
        search=search or ScriptedSearch(OFFICIAL),
        primary=primary,
        fallback=fallback,
        store=store,
        settings=settings,
    )
    return service, store, ctx, primary, fallback


async def test_happy_path_resolves_crawls_verifies(settings) -> None:
    service, store, ctx, primary, fallback = setup(settings=settings)
    budget = CreditBudget(total=50)
    outcome = await service.process(ctx.company_id, budget)

    assert outcome.domain_status is DomainResolutionStatus.VERIFIED
    assert outcome.domain == "drivn.in"
    assert outcome.provider_used == "firecrawl"
    assert outcome.used_fallback is False
    assert fallback.requests == []  # fallback only when needed
    assert outcome.credits_used == budget.spent == 2 + 4
    assert store.domains == {"drivn.in": ctx.company_id}
    (resolution,) = store.resolutions
    assert resolution.status is DomainResolutionStatus.RESOLVED
    assert any(
        c.get("excluded_reason") == "social platform (metadata only)" for c in resolution.candidates
    )
    assert {"social_link": "https://www.linkedin.com/company/drivn"} in resolution.candidates
    (crawl,) = store.crawls
    assert crawl.identity_verified is True and crawl.status is CrawlStatus.SUCCEEDED
    assert primary.requests[0].page_budget == settings.website_max_pages_per_company


async def test_ambiguous_domain_goes_to_review_without_crawling(settings) -> None:
    hits = [
        WebSearchHit(url="https://drivn.in/", title="Drivn", position=1),
        WebSearchHit(url="https://drivn.com/", title="Drivn", position=2),
    ]
    service, store, ctx, primary, fallback = setup(ScriptedSearch(hits), settings=settings)
    outcome = await service.process(ctx.company_id, CreditBudget(total=50))
    assert outcome.domain_status is DomainResolutionStatus.REVIEW
    assert primary.requests == [] and fallback.requests == []
    assert store.statuses[ctx.company_id][0] is DomainResolutionStatus.REVIEW
    assert store.domains == {}


async def test_publisher_is_never_treated_as_official_site(settings) -> None:
    hits = [WebSearchHit(url="https://news.example/drivn", title="Drivn raises", position=1)]
    service, _store, ctx, primary, _ = setup(ScriptedSearch(hits), settings=settings)
    outcome = await service.process(ctx.company_id, CreditBudget(total=50))
    assert outcome.domain_status is DomainResolutionStatus.NOT_FOUND
    assert primary.requests == []


async def test_fallback_runs_when_primary_insufficient(settings) -> None:
    service, _, ctx, _primary, fallback = setup(
        primary=ScriptedCrawler(ProviderName.FIRECRAWL, sufficient=False, credits=2),
        settings=settings,
    )
    outcome = await service.process(ctx.company_id, CreditBudget(total=50))
    assert fallback.requests, "fallback should run"
    assert outcome.used_fallback is True
    assert outcome.provider_used == "crawl4ai"
    assert outcome.domain_status is DomainResolutionStatus.VERIFIED
    assert outcome.attempts == ["firecrawl:insufficient", "crawl4ai:succeeded"]


async def test_fallback_runs_when_primary_fails(settings) -> None:
    service, store, ctx, _, _fallback = setup(
        primary=ScriptedCrawler(
            ProviderName.FIRECRAWL, error=ProviderUnavailableError("firecrawl", "down")
        ),
        settings=settings,
    )
    outcome = await service.process(ctx.company_id, CreditBudget(total=50))
    assert outcome.used_fallback
    failed = [c for c in store.crawls if c.status is CrawlStatus.FAILED]
    assert failed[0].error_code == "provider_unavailable"


async def test_no_fallback_when_not_operational(settings) -> None:
    service, _, ctx, _, fallback = setup(
        primary=ScriptedCrawler(ProviderName.FIRECRAWL, sufficient=False),
        fallback=ScriptedCrawler(ProviderName.CRAWL4AI, operational=False),
        settings=settings,
    )
    outcome = await service.process(ctx.company_id, CreditBudget(total=50))
    assert fallback.requests == []
    assert outcome.domain_status is DomainResolutionStatus.REVIEW


async def test_force_fallback_skips_primary(settings) -> None:
    service, _, ctx, primary, fallback = setup(settings=settings)
    outcome = await service.process(
        ctx.company_id, CreditBudget(total=50), domain_override="drivn.in", force_fallback=True
    )
    assert primary.requests == [] and fallback.requests
    assert outcome.credits_used == 0  # override + fallback-only = free
    assert outcome.domain_status is DomainResolutionStatus.VERIFIED


async def test_identity_mismatch_goes_to_review_and_is_not_assigned(settings) -> None:
    service, store, ctx, _, _ = setup(
        primary=ScriptedCrawler(ProviderName.FIRECRAWL, title="Unrelated Shoes Ltd"),
        settings=settings,
        name="Zypherion",
    )
    hits = [WebSearchHit(url="https://zypherion.com/", title="Zypherion", position=1)]
    service._search = ScriptedSearch(hits)
    # page text mentions "Drivn", not the company -> must not verify
    outcome = await service.process(ctx.company_id, CreditBudget(total=50))
    assert outcome.domain_status is DomainResolutionStatus.REVIEW
    assert "identity not confirmed" in (outcome.reason or "")
    assert store.domains == {}
    assert store.crawls[0].identity_verified is False


async def test_budget_exhausted_skips_paid_calls(settings) -> None:
    search = ScriptedSearch(OFFICIAL)
    service, _, ctx, primary, _ = setup(search, settings=settings)
    outcome = await service.process(ctx.company_id, CreditBudget(total=1))
    assert search.queries == [] and primary.requests == []
    assert "budget exhausted" in (outcome.reason or "")


async def test_low_remaining_budget_caps_primary_credits(settings) -> None:
    service, _, ctx, primary, _ = setup(settings=settings)
    await service.process(ctx.company_id, CreditBudget(total=6))  # 2 search -> 4 left
    assert primary.requests[0].max_credits == 4


async def test_search_failure_marks_failed(settings) -> None:
    service, store, ctx, primary, _ = setup(
        ScriptedSearch(ProviderUnavailableError("firecrawl", "timeout")), settings=settings
    )
    outcome = await service.process(ctx.company_id, CreditBudget(total=50))
    assert outcome.domain_status is DomainResolutionStatus.FAILED
    assert store.resolutions[0].error_code == "provider_unavailable"
    assert primary.requests == []


async def test_invalid_override_is_refused(settings) -> None:
    service, _, ctx, primary, _ = setup(settings=settings)
    outcome = await service.process(
        ctx.company_id, CreditBudget(total=50), domain_override="db.internal"
    )
    assert outcome.domain_status is DomainResolutionStatus.REVIEW
    assert primary.requests == []


async def test_second_company_with_same_verified_domain_is_merged(settings) -> None:
    service, store, ctx, _, _ = setup(settings=settings)
    await service.process(ctx.company_id, CreditBudget(total=50))
    twin = CompanyContext(
        company_id=uuid4(),
        name="Drivn Mobility",
        normalized_name="drivn mobility",
        domain=None,
        domain_status=DomainResolutionStatus.UNRESOLVED,
    )
    store.add_company(twin)
    outcome = await service.process(
        twin.company_id, CreditBudget(total=50), domain_override="drivn.in"
    )
    # homepage says "Drivn" - does not contain "drivnmobility", so identity is NOT verified:
    assert outcome.domain_status is DomainResolutionStatus.REVIEW
    assert store.merges == []  # never merge on name similarity


async def test_verified_twin_merges_into_existing_owner(settings) -> None:
    service, store, ctx, _, _ = setup(settings=settings)
    await service.process(ctx.company_id, CreditBudget(total=50))
    twin = CompanyContext(
        company_id=uuid4(),
        name="DRIVN",
        normalized_name="drivn",
        domain=None,
        domain_status=DomainResolutionStatus.UNRESOLVED,
    )
    store.add_company(twin)
    outcome = await service.process(
        twin.company_id, CreditBudget(total=50), domain_override="drivn.in"
    )
    assert outcome.domain_status is DomainResolutionStatus.VERIFIED
    assert outcome.merged_from == twin.company_id
    assert outcome.surviving_company_id == ctx.company_id
    assert store.merges == [(twin.company_id, ctx.company_id, "drivn.in")]


# ---------------------------------------------------------------- CLI safety
def _load_cli() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "run_website_intelligence", ROOT / "scripts" / "run_website_intelligence.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_cli_dry_run(capsys) -> None:
    assert await _load_cli().main([]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and '"domain_status": "verified"' in out


async def test_cli_refuses_live_without_switches(capsys, monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    assert await _load_cli().main(["--live"]) == 2
    out = capsys.readouterr().out
    assert "PROVIDER_MODE must be 'live'" in out
    assert "ALLOW_PAID_PROVIDER_CALLS must be true" in out


async def test_cli_fallback_only_does_not_require_paid_switch(
    capsys, monkeypatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PROVIDER_MODE", "live")
    code = await _load_cli().main(
        ["--live", "--company-id", str(uuid4()), "--domain", "drivn.in", "--force-fallback"]
    )
    out = capsys.readouterr().out
    assert code == 2
    assert "ALLOW_PAID_PROVIDER_CALLS" not in out  # free path
    assert "CRAWL4AI_BASE_URL must be set" in out


def test_domain_override_requires_single_company() -> None:
    import asyncio

    assert asyncio.run(_load_cli().main(["--domain", "drivn.in"])) == 2


@pytest.mark.parametrize(("total", "spend", "left"), [(10, 3, 7), (2, 5, 0)])
def test_credit_budget(total, spend, left) -> None:
    b = CreditBudget(total=total)
    b.spend(spend)
    assert b.remaining == left
