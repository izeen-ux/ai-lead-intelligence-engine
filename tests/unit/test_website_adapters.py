"""Firecrawl + Crawl4AI live adapters exercised through httpx2.MockTransport (no network)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx2
import pytest

from app.core.config import Settings
from app.core.errors import (
    ProviderCallBlockedError,
    ProviderError,
    ProviderNotConfiguredError,
    ProviderUnavailableError,
)
from app.domain.enums import PageType, ProviderName
from app.providers.contracts import WebSearchQuery, WebsiteExtractionRequest
from app.providers.crawl4ai.adapter import Crawl4AIAdapter
from app.providers.firecrawl.adapter import FirecrawlAdapter, search_credits

FC_KEY = "fc-TESTKEY0000000000000000000000000"
C4_TOKEN = "c4a-local-test-token-0000000000000000"
LONG = "Drivn builds electric mobility and AI automation for fleets. " * 10

Handler = Callable[[httpx2.Request], httpx2.Response]


class Recorder:
    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.handler(request)

    def bodies(self, path_suffix: str) -> list[dict[str, Any]]:
        return [
            json.loads(r.content)
            for r in self.requests
            if r.url.path.endswith(path_suffix) and r.content
        ]


def fc_settings(make_settings: Callable[..., Settings], **kw: Any) -> Settings:
    base = {
        "provider_mode": "live",
        "allow_paid_provider_calls": True,
        "firecrawl_api_key": FC_KEY,
    }
    base.update(kw)
    return make_settings(**base)


def firecrawl(settings, handler: Handler) -> tuple[FirecrawlAdapter, Recorder]:
    rec = Recorder(handler)
    return FirecrawlAdapter(settings, transport=httpx2.MockTransport(rec)), rec


def site_handler(
    map_links: list[Any] | None = None,
    scrape_status: dict[str, int] | None = None,
    map_status: int = 200,
) -> Handler:
    map_links = (
        map_links
        if map_links is not None
        else [
            {"url": "https://drivn.in/about"},
            {"url": "https://drivn.in/careers"},
            "https://drivn.in/pricing",
            {"url": "https://www.linkedin.com/company/drivn"},
            {"url": "https://inc42.com/drivn"},
        ]
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path.endswith("/map"):
            if map_status != 200:
                return httpx2.Response(map_status, json={"success": False, "error": "boom"})
            return httpx2.Response(200, json={"success": True, "links": map_links})
        if path.endswith("/scrape"):
            url = json.loads(request.content)["url"]
            status = (scrape_status or {}).get(url, 200)
            return httpx2.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "markdown": LONG if status < 400 else "Not found",
                        "links": ["https://www.linkedin.com/company/drivn", "https://drivn.in/x"],
                        "metadata": {
                            "title": ["Drivn - Home"] if url.endswith("/") else "Drivn page",
                            "description": "EV fleets",
                            "url": url,
                            "statusCode": status,
                        },
                    },
                },
            )
        if path.endswith("/credit-usage"):
            return httpx2.Response(200, json={"success": True, "data": {"remainingCredits": 993}})
        return httpx2.Response(404, json={"success": False})

    return handler


# ================================================================== Firecrawl: search
async def test_search_request_and_mapping(make_settings) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "success": True,
                "data": {
                    "web": [
                        {"url": "https://drivn.in/", "title": "Drivn", "description": "EV"},
                        {"url": "https://www.linkedin.com/company/drivn", "title": "LinkedIn"},
                        {"title": "no url - dropped"},
                    ]
                },
                "creditsUsed": 2,
            },
        )

    adapter, rec = firecrawl(fc_settings(make_settings), handler)
    result = await adapter.search_web(
        WebSearchQuery(query='"Drivn" official website', limit=5, country="IN")
    )
    assert [h.url for h in result.hits] == [
        "https://drivn.in/",
        "https://www.linkedin.com/company/drivn",
    ]
    assert [h.position for h in result.hits] == [1, 2]
    assert result.credits_used == 2
    body = rec.bodies("/search")[0]
    assert body == {
        "query": '"Drivn" official website',
        "limit": 5,
        "sources": ["web"],
        "ignoreInvalidURLs": True,
        "timeout": 60000,
        "country": "IN",
    }
    assert "scrapeOptions" not in body  # never pay for scraping search results
    request = rec.requests[0]
    assert request.url.host == "api.firecrawl.dev"
    assert request.url.path == "/v2/search"
    assert request.headers["authorization"] == f"Bearer {FC_KEY}"


def test_search_credit_formula() -> None:
    assert [search_credits(n) for n in (1, 5, 10, 11, 20)] == [2, 2, 2, 4, 4]


async def test_search_blocked_when_over_company_cap(make_settings) -> None:
    adapter, rec = firecrawl(
        fc_settings(make_settings, firecrawl_max_credits_per_company=1), site_handler()
    )
    with pytest.raises(ProviderCallBlockedError, match="search would use 2 credits"):
        await adapter.search_web(WebSearchQuery(query="x", limit=5))
    assert rec.requests == []


async def test_paid_guard_blocks_before_any_http(make_settings) -> None:
    s = make_settings(provider_mode="live", firecrawl_api_key=FC_KEY)  # paid calls not allowed
    adapter, rec = firecrawl(s, site_handler())
    with pytest.raises(ProviderCallBlockedError):
        await adapter.search_web(WebSearchQuery(query="x"))
    with pytest.raises(ProviderCallBlockedError):
        await adapter.extract_site(WebsiteExtractionRequest(domain="drivn.in"))
    assert rec.requests == []


async def test_missing_key_is_not_configured(make_settings) -> None:
    s = make_settings(provider_mode="live", allow_paid_provider_calls=True)
    adapter, rec = firecrawl(s, site_handler())
    with pytest.raises(ProviderNotConfiguredError):
        await adapter.search_web(WebSearchQuery(query="x"))
    assert rec.requests == []


# ================================================================== Firecrawl: extract_site
async def test_extract_site_maps_then_scrapes_budgeted_pages(make_settings) -> None:
    adapter, rec = firecrawl(fc_settings(make_settings), site_handler())
    result = await adapter.extract_site(
        WebsiteExtractionRequest(domain="https://www.drivn.in", page_budget=8)
    )

    assert result.provider is ProviderName.FIRECRAWL
    assert result.domain == "drivn.in"
    scraped = [b["url"] for b in rec.bodies("/scrape")]
    assert scraped == [
        "https://drivn.in/",
        "https://drivn.in/about",
        "https://drivn.in/careers",
        "https://drivn.in/pricing",
    ]
    assert not any(
        "linkedin" in u or "inc42" in u for u in scraped
    )  # social/off-site never fetched
    for body in rec.bodies("/scrape"):
        assert body["formats"] == ["markdown", "links"]  # no json (+4 credits)
        assert body["parsers"] == []  # no PDF parsing (+1 credit)
        assert body["onlyMainContent"] is True
    map_body = rec.bodies("/map")[0]
    assert map_body["url"] == "https://drivn.in"
    assert map_body["includeSubdomains"] is False
    assert result.credits_used == 1 + 4
    assert result.pages_attempted == 4
    assert [p.page_type for p in result.pages] == [
        PageType.HOME,
        PageType.ABOUT,
        PageType.CAREERS,
        PageType.PRICING,
    ]
    assert result.pages[0].title == "Drivn - Home"  # list-valued metadata handled
    assert result.sufficient is True
    assert result.social_links == {"linkedin.com": "https://www.linkedin.com/company/drivn"}
    assert result.facts["has_careers_page"] is True


@pytest.mark.parametrize(("budget", "cap", "expected_scrapes"), [(2, 15, 2), (8, 3, 2), (8, 5, 4)])
async def test_page_and_credit_budgets_are_enforced(
    make_settings, budget, cap, expected_scrapes
) -> None:
    adapter, rec = firecrawl(
        fc_settings(make_settings, firecrawl_max_credits_per_company=cap), site_handler()
    )
    result = await adapter.extract_site(
        WebsiteExtractionRequest(domain="drivn.in", page_budget=budget)
    )
    assert len(rec.bodies("/scrape")) == expected_scrapes
    assert result.credits_used <= cap


async def test_settings_page_ceiling_beats_request(make_settings) -> None:
    adapter, rec = firecrawl(
        fc_settings(make_settings, website_max_pages_per_company=1), site_handler()
    )
    await adapter.extract_site(WebsiteExtractionRequest(domain="drivn.in", page_budget=20))
    assert len(rec.bodies("/scrape")) == 1


async def test_credit_cap_too_small_refuses(make_settings) -> None:
    adapter, rec = firecrawl(fc_settings(make_settings), site_handler())
    with pytest.raises(ProviderCallBlockedError, match="credit cap too small"):
        await adapter.extract_site(WebsiteExtractionRequest(domain="drivn.in", max_credits=1))
    assert rec.requests == []


async def test_error_pages_count_as_failed_not_content(make_settings) -> None:
    adapter, _ = firecrawl(
        fc_settings(make_settings), site_handler(scrape_status={"https://drivn.in/about": 404})
    )
    result = await adapter.extract_site(WebsiteExtractionRequest(domain="drivn.in"))
    assert "https://drivn.in/about" in result.failed_urls
    assert all(p.page_type is not PageType.ABOUT for p in result.pages)
    assert result.credits_used == 1 + 4  # the 404 document was still billed


async def test_map_failure_still_scrapes_homepage(make_settings) -> None:
    adapter, rec = firecrawl(fc_settings(make_settings), site_handler(map_status=500))
    result = await adapter.extract_site(WebsiteExtractionRequest(domain="drivn.in"))
    assert [b["url"] for b in rec.bodies("/scrape")] == ["https://drivn.in/"]
    assert result.credits_used == 1  # failed map not billed


async def test_refuses_to_crawl_internal_or_social_domains(make_settings) -> None:
    adapter, rec = firecrawl(fc_settings(make_settings), site_handler())
    for domain in ("linkedin.com", "intranet.local"):
        with pytest.raises(ValueError, match="not allowed"):
            await adapter.extract_site(WebsiteExtractionRequest(domain=domain))
    assert rec.requests == []


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, ProviderNotConfiguredError),
        (402, ProviderCallBlockedError),
        (429, ProviderUnavailableError),
        (503, ProviderUnavailableError),
        (400, ProviderError),
    ],
)
async def test_http_errors_are_mapped_and_key_scrubbed(make_settings, status, expected) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, json={"success": False, "error": f"bad key {FC_KEY}"})

    adapter, _ = firecrawl(fc_settings(make_settings), handler)
    with pytest.raises(expected) as exc_info:
        await adapter.search_web(WebSearchQuery(query="x"))
    assert exc_info.value.details["http_status"] == status
    assert FC_KEY not in json.dumps(exc_info.value.details) + exc_info.value.message


async def test_transport_errors_are_retryable(make_settings) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("down", request=request)

    adapter, _ = firecrawl(fc_settings(make_settings), handler)
    with pytest.raises(ProviderUnavailableError) as exc_info:
        await adapter.search_web(WebSearchQuery(query="x"))
    assert exc_info.value.retryable


async def test_remaining_credits(make_settings) -> None:
    adapter, rec = firecrawl(fc_settings(make_settings), site_handler())
    assert await adapter.remaining_credits() == 993
    assert rec.requests[0].method == "GET"
    assert rec.requests[0].url.path == "/v2/team/credit-usage"


async def test_self_hosted_base_url(make_settings) -> None:
    s = fc_settings(make_settings, firecrawl_base_url="http://127.0.0.1:3002/v2")
    adapter, rec = firecrawl(s, site_handler())
    await adapter.remaining_credits()
    assert str(rec.requests[0].url) == "http://127.0.0.1:3002/v2/team/credit-usage"


# ================================================================== Crawl4AI
def c4_settings(make_settings: Callable[..., Settings], **kw: Any) -> Settings:
    base = {
        "provider_mode": "live",
        "crawl4ai_base_url": "http://127.0.0.1:11235",
        "crawl4ai_api_token": C4_TOKEN,
    }
    base.update(kw)
    return make_settings(**base)


def c4_result(url: str, *, ok: bool = True, internal: list[str] | None = None) -> dict[str, Any]:
    return {
        "url": url,
        "success": ok,
        "status_code": 200 if ok else 500,
        "markdown": {"raw_markdown": LONG if ok else "", "fit_markdown": ""},
        "links": {
            "internal": [{"href": h, "text": ""} for h in (internal or [])],
            "external": [{"href": "https://www.linkedin.com/company/drivn", "text": "in"}],
        },
        "metadata": {"title": "Drivn", "description": None},
        "error_message": "" if ok else "boom",
    }


def c4_handler(home_ok: bool = True, status: int = 200) -> Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if status != 200:
            return httpx2.Response(status, json={"detail": "nope"})
        urls = json.loads(request.content)["urls"]
        if urls == ["https://drivn.in/"]:
            return httpx2.Response(
                200,
                json={
                    "success": True,
                    "results": [
                        c4_result(
                            urls[0],
                            ok=home_ok,
                            internal=["https://drivn.in/about", "https://drivn.in/careers"],
                        )
                    ],
                },
            )
        return httpx2.Response(200, json={"success": True, "results": [c4_result(u) for u in urls]})

    return handler


def crawl4ai(settings, handler: Handler) -> tuple[Crawl4AIAdapter, Recorder]:
    rec = Recorder(handler)
    return Crawl4AIAdapter(settings, transport=httpx2.MockTransport(rec)), rec


async def test_crawl4ai_home_then_selected_pages(make_settings) -> None:
    adapter, rec = crawl4ai(c4_settings(make_settings), c4_handler())
    result = await adapter.extract_site(WebsiteExtractionRequest(domain="drivn.in", page_budget=3))
    bodies = rec.bodies("/crawl")
    assert bodies[0]["urls"] == ["https://drivn.in/"]
    assert bodies[1]["urls"] == ["https://drivn.in/about", "https://drivn.in/careers"]
    assert bodies[0]["crawler_config"]["type"] == "CrawlerRunConfig"
    assert rec.requests[0].headers["authorization"] == f"Bearer {C4_TOKEN}"
    assert [p.page_type for p in result.pages] == [PageType.HOME, PageType.ABOUT, PageType.CAREERS]
    assert result.provider is ProviderName.CRAWL4AI
    assert result.credits_used == 0
    assert result.sufficient
    assert result.social_links == {"linkedin.com": "https://www.linkedin.com/company/drivn"}


async def test_crawl4ai_budget_one_fetches_only_home(make_settings) -> None:
    adapter, rec = crawl4ai(c4_settings(make_settings), c4_handler())
    await adapter.extract_site(WebsiteExtractionRequest(domain="drivn.in", page_budget=1))
    assert len(rec.requests) == 1


async def test_crawl4ai_failed_home_is_insufficient(make_settings) -> None:
    adapter, _ = crawl4ai(c4_settings(make_settings), c4_handler(home_ok=False))
    result = await adapter.extract_site(WebsiteExtractionRequest(domain="drivn.in", page_budget=1))
    assert not result.sufficient
    assert result.failed_urls == ["https://drivn.in/"]


async def test_crawl4ai_requires_base_url(make_settings) -> None:
    adapter, rec = crawl4ai(make_settings(provider_mode="live"), c4_handler())
    assert adapter.status().operational is False
    with pytest.raises(ProviderNotConfiguredError, match="CRAWL4AI_BASE_URL"):
        await adapter.extract_site(WebsiteExtractionRequest(domain="drivn.in"))
    assert rec.requests == []


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, ProviderNotConfiguredError), (500, ProviderUnavailableError), (422, ProviderError)],
)
async def test_crawl4ai_errors(make_settings, status, expected) -> None:
    adapter, _ = crawl4ai(c4_settings(make_settings), c4_handler(status=status))
    with pytest.raises(expected):
        await adapter.extract_site(WebsiteExtractionRequest(domain="drivn.in"))


async def test_crawl4ai_server_down(make_settings) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    adapter, _ = crawl4ai(c4_settings(make_settings), handler)
    with pytest.raises(ProviderUnavailableError, match="container running"):
        await adapter.extract_site(WebsiteExtractionRequest(domain="drivn.in"))


async def test_crawl4ai_is_free_so_not_paid_guarded(make_settings) -> None:
    adapter, rec = crawl4ai(
        c4_settings(make_settings, allow_paid_provider_calls=False), c4_handler()
    )
    result = await adapter.extract_site(WebsiteExtractionRequest(domain="drivn.in", page_budget=1))
    assert result.pages and rec.requests
