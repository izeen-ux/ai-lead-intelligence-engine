from __future__ import annotations

from pydantic import HttpUrl

from app.domain.enums import PageType, ProviderName
from app.domain.website import assess_sufficiency, build_site_facts
from app.providers.base import MockProviderBase
from app.providers.contracts import WebPage, WebsiteExtractionRequest, WebsiteExtractionResult


class MockCrawl4AIProvider(MockProviderBase):
    """Offline browser-rendered fallback: always returns a sufficient homepage."""

    name = ProviderName.CRAWL4AI

    async def extract_site(self, request: WebsiteExtractionRequest) -> WebsiteExtractionResult:
        self.calls.append("extract_site")
        label = request.domain.split(".")[0]
        pages = [
            WebPage(
                url=HttpUrl(f"https://{request.domain}/"),
                page_type=PageType.HOME,
                markdown=f"# {label}\n\nBrowser-rendered content for {label}. " * 20,
                title=label,
                status_code=200,
            )
        ][: request.page_budget]
        sufficient, reason = assess_sufficiency(pages)
        return WebsiteExtractionResult(
            provider=self.name,
            domain=request.domain,
            pages=pages,
            facts=build_site_facts(pages),
            sufficient=sufficient,
            reason=reason,
            pages_attempted=1,
        )
