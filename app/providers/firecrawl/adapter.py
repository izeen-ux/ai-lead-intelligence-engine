"""Live Firecrawl adapter (Phase 3) - direct REST v2 calls via ``httpx2``.

Endpoints (verified against docs.firecrawl.dev on 2026-09-29):
  POST /v2/search  {query, limit, country, sources, ignoreInvalidURLs, timeout}
       -> {success, data: {web: [{url, title, description, position?}]}, creditsUsed}
       cost: 2 credits per 10 results (no scrapeOptions -> no extra scrape charge)
  POST /v2/map     {url, limit, includeSubdomains, sitemap, ignoreQueryParameters, timeout}
       -> {success, links: [{url, title?, description?}]}              cost: 1 credit
  POST /v2/scrape  {url, formats, onlyMainContent, timeout, parsers, blockAds, ...}
       -> {success, data: {markdown, links, metadata: {title, description, sourceURL,
           statusCode, error}}}                                         cost: 1 credit / page
  GET  /v2/team/credit-usage -> {success, data: {remainingCredits, planCredits, ...}}  (free)

Why not the SDK: we need three endpoints; the SDK pulls nest-asyncio (patches the event loop
globally), aiohttp, requests and websockets (ADR-025).

Budget: worst case = 1 (map) + pages; pages are reduced to fit the per-company credit cap and
the run is refused when even the homepage does not fit. JSON-extraction / PDF parsing
(+4 / +1 credits) are never requested.
"""

from __future__ import annotations

import math
from typing import Any

import httpx2

from app.core.config import Settings
from app.core.errors import (
    ProviderCallBlockedError,
    ProviderError,
    ProviderNotConfiguredError,
    ProviderUnavailableError,
)
from app.core.keyring import KeyRing, ring_for
from app.core.logging import get_logger
from app.domain.enums import ProviderName
from app.domain.website import (
    assess_sufficiency,
    build_site_facts,
    crawlable_domain,
    extract_social_links,
    select_pages,
)
from app.providers.base import LiveAdapterBase
from app.providers.contracts import (
    WebPage,
    WebSearchHit,
    WebSearchQuery,
    WebSearchResult,
    WebsiteExtractionRequest,
    WebsiteExtractionResult,
)

logger = get_logger(__name__)

DEFAULT_BASE_URL = "https://api.firecrawl.dev/v2"
MAP_CREDITS = 1
SCRAPE_CREDITS = 1
MAX_MARKDOWN_CHARS = 200_000
MAP_LINK_LIMIT = 100


def search_credits(limit: int) -> int:
    """Firecrawl bills search at 2 credits per 10 results, rounded up."""
    return 2 * math.ceil(limit / 10)


def _first(value: Any) -> str | None:
    """Firecrawl metadata fields may be a string or a list of strings."""
    if isinstance(value, list):
        value = value[0] if value else None
    return str(value).strip() if value else None


class FirecrawlAdapter(LiveAdapterBase):
    name = ProviderName.FIRECRAWL
    implemented_in_phase = 3
    operational = True

    def __init__(
        self, settings: Settings, *, transport: httpx2.AsyncBaseTransport | None = None
    ) -> None:
        super().__init__(settings)
        self._transport = transport  # injected in tests (httpx2.MockTransport)

    # ------------------------------------------------------------------ http plumbing
    def _client(self) -> httpx2.AsyncClient:
        if self._ring().current() is None:  # pragma: no cover - _ensure_callable checked
            raise ProviderNotConfiguredError(self.name.value, "FIRECRAWL_API_KEY missing")
        return httpx2.AsyncClient(  # the key is sent per request: it can change (backup keys)
            base_url=(self._settings.firecrawl_base_url or DEFAULT_BASE_URL).rstrip("/") + "/",
            timeout=self._settings.firecrawl_timeout_seconds + 15,
            transport=self._transport,
        )

    def _error(self, operation: str, status: int, body: Any) -> ProviderError:
        message = None
        if isinstance(body, dict):
            message = body.get("error") or body.get("message")
        details = {"operation": operation, "http_status": status}
        if message:
            details["api_message"] = self._scrub(str(message))[:300]
        if status in (401, 403):
            return ProviderNotConfiguredError(
                self.name.value, "Firecrawl rejected the API key", details=details
            )
        if status == 402:
            return ProviderCallBlockedError(
                self.name.value, "Firecrawl credits exhausted (HTTP 402)", details=details
            )
        if status == 429 or status >= 500:
            return ProviderUnavailableError(
                self.name.value, f"Firecrawl temporarily unavailable ({status})", details=details
            )
        return ProviderError(
            self.name.value, f"Firecrawl {operation} failed ({status})", details=details
        )

    def _ring(self) -> KeyRing:
        return ring_for(self.name.value, self._settings.provider_keys("firecrawl_api_key"))

    def _scrub(self, text: str) -> str:
        for key in self._settings.provider_keys("firecrawl_api_key"):
            text = text.replace(key, "***REDACTED***")
        return text

    async def _request(
        self,
        client: httpx2.AsyncClient,
        method: str,
        path: str,
        operation: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ring = self._ring()
        key = ring.current() or ""
        try:
            response = await client.request(
                method, path, json=body, headers={"Authorization": f"Bearer {key}"}
            )
        except httpx2.TimeoutException:
            raise ProviderUnavailableError(
                self.name.value,
                f"Firecrawl {operation} timed out",
                details={"operation": operation},
            ) from None
        except httpx2.TransportError as exc:
            raise ProviderUnavailableError(
                self.name.value,
                f"network error talking to Firecrawl: {type(exc).__name__}",
                details={"operation": operation},
            ) from None
        try:
            payload: Any = response.json()
        except ValueError:
            payload = None
        if response.status_code in (401, 402, 403) and ring.bench(
            key, rejected=response.status_code != 402
        ):
            # rejected key / credits used up, and a backup key exists: same request, next key
            return await self._request(client, method, path, operation, body)
        if response.status_code >= 400:
            raise self._error(operation, response.status_code, payload)
        if not isinstance(payload, dict) or payload.get("success") is False:
            raise ProviderError(
                self.name.value,
                f"Firecrawl {operation} returned an unsuccessful payload",
                details={"operation": operation, "http_status": response.status_code},
            )
        return payload

    def _timeout_ms(self) -> int:
        return self._settings.firecrawl_timeout_seconds * 1000

    # ------------------------------------------------------------------ search
    async def search_web(self, query: WebSearchQuery) -> WebSearchResult:
        self._ensure_callable("search_web")
        estimate = search_credits(query.limit)
        cap = self._settings.firecrawl_max_credits_per_company
        if estimate > cap:
            raise ProviderCallBlockedError(
                self.name.value,
                f"search would use {estimate} credits, above FIRECRAWL_MAX_CREDITS_PER_COMPANY",
                details={"estimated_credits": estimate, "cap": cap},
            )
        body: dict[str, Any] = {
            "query": query.query,
            "limit": query.limit,
            "sources": ["web"],
            "ignoreInvalidURLs": True,
            "timeout": self._timeout_ms(),
        }
        if query.country:
            body["country"] = query.country
        async with self._client() as client:
            payload = await self._request(client, "POST", "search", "search", body)
        data = payload.get("data")
        raw_hits = data.get("web", []) if isinstance(data, dict) else data or []
        hits = [
            WebSearchHit(
                url=str(h.get("url")),
                title=_first(h.get("title")),
                description=_first(h.get("description")),
                position=i,
            )
            for i, h in enumerate(raw_hits[: query.limit], start=1)
            if isinstance(h, dict) and h.get("url")
        ]
        credits = payload.get("creditsUsed")
        return WebSearchResult(
            provider=self.name,
            query=query.query,
            hits=hits,
            credits_used=int(credits) if isinstance(credits, int | float) else estimate,
        )

    # ------------------------------------------------------------------ website extraction
    def plan_budget(self, request: WebsiteExtractionRequest) -> tuple[int, int]:
        """Return ``(pages, credit_cap)``: pages reduced so map + pages fits the credit cap."""
        s = self._settings
        pages = min(request.page_budget, s.website_max_pages_per_company)
        cap = min(
            request.max_credits or s.firecrawl_max_credits_per_company,
            s.firecrawl_max_credits_per_company,
        )
        pages = min(pages, cap - MAP_CREDITS)
        if pages < 1:
            raise ProviderCallBlockedError(
                self.name.value,
                "credit cap too small to scrape even the homepage",
                details={"credit_cap": cap},
            )
        return pages, cap

    async def extract_site(self, request: WebsiteExtractionRequest) -> WebsiteExtractionResult:
        self._ensure_callable("extract_site")
        domain = crawlable_domain(request.domain)
        pages_budget, cap = self.plan_budget(request)
        credits = 0
        pages: list[WebPage] = []
        failed: list[str] = []
        async with self._client() as client:
            discovered: list[str] = []
            try:
                mapped = await self._request(
                    client,
                    "POST",
                    "map",
                    "map",
                    {
                        "url": f"https://{domain}",
                        "limit": MAP_LINK_LIMIT,
                        "includeSubdomains": False,
                        "ignoreQueryParameters": True,
                        "sitemap": "include",
                        "timeout": self._timeout_ms(),
                    },
                )
                credits += MAP_CREDITS
                for link in mapped.get("links") or []:
                    url = link.get("url") if isinstance(link, dict) else link
                    if isinstance(url, str):
                        discovered.append(url)
            except ProviderUnavailableError as exc:
                # Map is an optimisation; without it we still scrape the homepage.
                logger.warning("firecrawl map failed", extra={"error_code": exc.code})
            except (ProviderNotConfiguredError, ProviderCallBlockedError):
                raise
            except ProviderError as exc:
                logger.warning("firecrawl map failed", extra={"error_code": exc.code})

            plan = select_pages(domain, discovered, pages_budget)
            for planned in plan:
                if credits + SCRAPE_CREDITS > cap:
                    break  # never exceed the per-company credit cap
                try:
                    scraped = await self._request(
                        client,
                        "POST",
                        "scrape",
                        "scrape",
                        {
                            "url": planned.url,
                            "formats": ["markdown", "links"],
                            "onlyMainContent": True,
                            "timeout": self._timeout_ms(),
                            "parsers": [],
                            "blockAds": True,
                            "removeBase64Images": True,
                        },
                    )
                except (ProviderNotConfiguredError, ProviderCallBlockedError):
                    raise
                except ProviderError:
                    failed.append(planned.url)
                    continue
                data = scraped.get("data") or {}
                credits += SCRAPE_CREDITS  # a document was returned -> billed
                meta = data.get("metadata") or {}
                status = meta.get("statusCode")
                markdown = str(data.get("markdown") or "")
                if (isinstance(status, int) and status >= 400) or not markdown.strip():
                    failed.append(planned.url)
                    continue
                pages.append(
                    WebPage(
                        url=_first(meta.get("url")) or planned.url,  # type: ignore[arg-type]
                        page_type=planned.page_type,
                        markdown=markdown[:MAX_MARKDOWN_CHARS],
                        title=_first(meta.get("title")),
                        description=_first(meta.get("description")),
                        status_code=status if isinstance(status, int) else None,
                        links=[str(u) for u in (data.get("links") or []) if isinstance(u, str)],
                    )
                )

        sufficient, reason = assess_sufficiency(pages)
        all_links = [link for p in pages for link in p.links] + discovered
        return WebsiteExtractionResult(
            provider=self.name,
            domain=domain,
            pages=pages,
            facts=build_site_facts(pages),
            social_links=extract_social_links(all_links),
            sufficient=sufficient,
            reason=reason,
            pages_attempted=len(plan),
            failed_urls=failed,
            credits_used=credits,
        )

    # ------------------------------------------------------------------ usage (free)
    async def remaining_credits(self) -> int | None:
        """Team credit balance (free endpoint) - used to verify actual spend."""
        self._ensure_callable("remaining_credits")
        async with self._client() as client:
            payload = await self._request(client, "GET", "team/credit-usage", "credit_usage")
        data = payload.get("data") or {}
        value = data.get("remainingCredits") if isinstance(data, dict) else None
        return int(value) if isinstance(value, int | float) else None
