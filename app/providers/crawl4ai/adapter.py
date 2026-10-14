"""Live Crawl4AI adapter (Phase 3) - fallback crawler via the official Docker server.

Runtime: ``unclecode/crawl4ai:0.9.4`` container bound to ``127.0.0.1:11235`` (ADR-026). The
heavy browser/LLM dependencies stay inside the container, not in this app's environment.
Verified against the running server on 2026-09-29:

  GET  /health -> {"status": "ok", "version": "0.9.4"}          (no auth)
  POST /crawl  (Authorization: Bearer <CRAWL4AI_API_TOKEN>; 401 without it)
       {"urls": [...], "browser_config": {"type": "BrowserConfig", "params": {...}},
        "crawler_config": {"type": "CrawlerRunConfig", "params": {...}}}
    -> {"success": bool, "results": [{url, success, status_code, redirected_url,
        markdown: {raw_markdown, ...}, links: {internal: [{href, text}], external: [...]},
        metadata: {title, description}, error_message}]}

Only used when Firecrawl is insufficient or failed. Free (local compute) but still bounded by
the same page budget; social URLs are never fetched.
"""

from __future__ import annotations

from typing import Any

import httpx2

from app.core.config import Settings
from app.core.errors import ProviderError, ProviderNotConfiguredError, ProviderUnavailableError
from app.domain.enums import PageType, ProviderName
from app.domain.website import (
    assess_sufficiency,
    build_site_facts,
    crawlable_domain,
    extract_social_links,
    select_pages,
)
from app.providers.base import LiveAdapterBase
from app.providers.contracts import (
    ProviderStatus,
    WebPage,
    WebsiteExtractionRequest,
    WebsiteExtractionResult,
)

MAX_MARKDOWN_CHARS = 200_000


def _markdown(result: dict[str, Any]) -> str:
    md = result.get("markdown")
    if isinstance(md, dict):
        return str(md.get("raw_markdown") or "")
    return str(md or "")


def _hrefs(result: dict[str, Any]) -> tuple[list[str], list[str]]:
    links = result.get("links") or {}

    def pick(kind: str) -> list[str]:
        return [
            str(item.get("href"))
            for item in links.get(kind) or []
            if isinstance(item, dict) and item.get("href")
        ]

    return pick("internal"), pick("external")


class Crawl4AIAdapter(LiveAdapterBase):
    name = ProviderName.CRAWL4AI
    implemented_in_phase = 3
    operational = True

    def __init__(
        self, settings: Settings, *, transport: httpx2.AsyncBaseTransport | None = None
    ) -> None:
        super().__init__(settings)
        self._transport = transport

    def status(self) -> ProviderStatus:
        base = super().status()
        configured = bool(self._settings.crawl4ai_base_url)
        return base.model_copy(
            update={
                "configured": configured,
                "operational": configured,
                "detail": None if configured else "CRAWL4AI_BASE_URL not set",
            }
        )

    def _ensure_callable(self, operation: str) -> None:
        super()._ensure_callable(operation)
        if not self._settings.crawl4ai_base_url:
            raise ProviderNotConfiguredError(
                self.name.value,
                f"{operation} requires CRAWL4AI_BASE_URL (local Crawl4AI Docker server)",
                details={"operation": operation},
            )

    def _client(self) -> httpx2.AsyncClient:
        headers = {}
        token = self._settings.crawl4ai_api_token
        if token is not None and token.get_secret_value():
            headers["Authorization"] = f"Bearer {token.get_secret_value()}"
        return httpx2.AsyncClient(
            base_url=str(self._settings.crawl4ai_base_url).rstrip("/"),
            headers=headers,
            timeout=self._settings.crawl4ai_timeout_seconds + 30,
            transport=self._transport,
        )

    async def _crawl(self, client: httpx2.AsyncClient, urls: list[str]) -> list[dict[str, Any]]:
        body = {
            "urls": urls,
            "browser_config": {"type": "BrowserConfig", "params": {"headless": True}},
            "crawler_config": {
                "type": "CrawlerRunConfig",
                "params": {
                    "stream": False,
                    "cache_mode": "bypass",
                    "page_timeout": self._settings.crawl4ai_timeout_seconds * 1000,
                },
            },
        }
        try:
            response = await client.post("/crawl", json=body)
        except httpx2.TimeoutException:
            raise ProviderUnavailableError(self.name.value, "Crawl4AI crawl timed out") from None
        except httpx2.TransportError as exc:
            raise ProviderUnavailableError(
                self.name.value,
                f"Crawl4AI server unreachable ({type(exc).__name__}); is the container running?",
            ) from None
        if response.status_code in (401, 403):
            raise ProviderNotConfiguredError(
                self.name.value, "Crawl4AI server rejected the token (CRAWL4AI_API_TOKEN)"
            )
        if response.status_code >= 500 or response.status_code == 429:
            raise ProviderUnavailableError(
                self.name.value, f"Crawl4AI server error ({response.status_code})"
            )
        if response.status_code >= 400:
            raise ProviderError(
                self.name.value,
                f"Crawl4AI request rejected ({response.status_code})",
                details={"http_status": response.status_code},
            )
        payload = response.json()
        results = payload.get("results") if isinstance(payload, dict) else None
        return [r for r in results or [] if isinstance(r, dict)]

    @staticmethod
    def _to_page(result: dict[str, Any], page_type: PageType) -> WebPage | None:
        status = result.get("status_code")
        markdown = _markdown(result)
        if not result.get("success") or not markdown.strip():
            return None
        if isinstance(status, int) and status >= 400:
            return None
        meta = result.get("metadata") or {}
        internal, external = _hrefs(result)
        return WebPage(
            url=str(result.get("redirected_url") or result.get("url")),  # type: ignore[arg-type]
            page_type=page_type,
            markdown=markdown[:MAX_MARKDOWN_CHARS],
            title=(meta.get("title") or None),
            description=(meta.get("description") or None),
            status_code=status if isinstance(status, int) else None,
            links=internal + external,
        )

    async def extract_site(self, request: WebsiteExtractionRequest) -> WebsiteExtractionResult:
        self._ensure_callable("extract_site")
        domain = crawlable_domain(request.domain)
        budget = min(request.page_budget, self._settings.website_max_pages_per_company)
        pages: list[WebPage] = []
        failed: list[str] = []
        home_url = f"https://{domain}/"
        async with self._client() as client:
            home_results = await self._crawl(client, [home_url])
            home = self._to_page(home_results[0], PageType.HOME) if home_results else None
            internal = _hrefs(home_results[0])[0] if home_results else []
            if home is None:
                failed.append(home_url)
            else:
                pages.append(home)
            plan = select_pages(domain, internal, budget)
            rest = plan[1:]  # homepage already fetched
            if rest:
                results = await self._crawl(client, [p.url for p in rest])
                by_url = {str(r.get("url")): r for r in results}
                for planned in rest:
                    result = by_url.get(planned.url)
                    page = self._to_page(result, planned.page_type) if result else None
                    if page is None:
                        failed.append(planned.url)
                    else:
                        pages.append(page)

        sufficient, reason = assess_sufficiency(pages)
        return WebsiteExtractionResult(
            provider=self.name,
            domain=domain,
            pages=pages,
            facts=build_site_facts(pages),
            social_links=extract_social_links([link for p in pages for link in p.links]),
            sufficient=sufficient,
            reason=reason,
            pages_attempted=len(plan) if plan else 1,
            failed_urls=failed,
            credits_used=0,
        )
