from __future__ import annotations

from pydantic import HttpUrl

from app.domain.enums import ProviderName
from app.domain.website import assess_sufficiency, build_site_facts, select_pages
from app.providers.base import MockProviderBase
from app.providers.contracts import (
    WebPage,
    WebSearchHit,
    WebSearchQuery,
    WebSearchResult,
    WebsiteExtractionRequest,
    WebsiteExtractionResult,
)

_MOCK_SITE_PATHS = ("/about", "/products", "/careers", "/pricing", "/blog", "/contact")


class MockFirecrawlProvider(MockProviderBase):
    """Offline Firecrawl. Search returns the company's '.example' site plus noise; domains
    starting with ``js-heavy`` return ``sufficient=False`` to exercise the fallback."""

    name = ProviderName.FIRECRAWL

    async def search_web(self, query: WebSearchQuery) -> WebSearchResult:
        self.calls.append("search_web")
        words = [w for w in query.query.replace('"', " ").split() if w.isalnum()]
        slug = "".join(w.lower() for w in words[:2]) or "company"
        name = " ".join(words[:2]) or "Company"
        hits = [
            WebSearchHit(url="https://www.linkedin.com/company/x", title=name, position=1),
            WebSearchHit(
                url=f"https://{slug}.example/", title=f"{name} - Official site", position=2
            ),
            WebSearchHit(url="https://news.example/story", title=f"{name} raises", position=3),
        ]
        return WebSearchResult(
            provider=self.name, query=query.query, hits=hits[: query.limit], credits_used=2
        )

    async def extract_site(self, request: WebsiteExtractionRequest) -> WebsiteExtractionResult:
        self.calls.append("extract_site")
        domain = request.domain
        if domain.startswith("js-heavy"):
            return WebsiteExtractionResult(
                provider=self.name,
                domain=domain,
                sufficient=False,
                reason="homepage missing or nearly empty (possible JavaScript-rendered site)",
                pages_attempted=1,
                failed_urls=[f"https://{domain}/"],
                credits_used=2,
            )
        plan = select_pages(
            domain, [f"https://{domain}{p}" for p in _MOCK_SITE_PATHS], request.page_budget
        )
        label = domain.split(".")[0]
        pages = [
            WebPage(
                url=HttpUrl(p.url),
                page_type=p.page_type,
                markdown=f"# {label}\n\n{label} builds AI automation software. " * 20,
                title=f"{label} - {p.page_type.value}",
                status_code=200,
                links=["https://www.linkedin.com/company/" + label],
            )
            for p in plan
        ]
        sufficient, reason = assess_sufficiency(pages)
        return WebsiteExtractionResult(
            provider=self.name,
            domain=domain,
            pages=pages,
            facts=build_site_facts(pages),
            social_links={"linkedin.com": "https://www.linkedin.com/company/" + label},
            sufficient=sufficient,
            reason=reason,
            pages_attempted=len(plan),
            credits_used=1 + len(pages),
        )
