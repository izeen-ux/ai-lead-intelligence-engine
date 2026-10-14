"""Actor specs: the ONLY place that knows a specific Actor's input keys and output JSON.

An ``ActorSpec`` builds the Actor input from a ``DiscoveryQuery`` and normalizes raw dataset
items into ``CandidateRecord``s. Adding another Actor = adding another spec; the adapter,
service and database never see raw Actor JSON.

Input keys below were verified against the live Actor input schema via the Apify API on
2026-09-28 (``apify/google-search-scraper``, build ``lNbEbWu4zO04U9Qdw``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar, Protocol
from urllib.parse import urlsplit

from pydantic import ValidationError

from app.core.errors import DomainValidationError
from app.domain.enums import SourceType
from app.providers.contracts import CandidateRecord, DiscoveryQuery
from app.utils.funding_headlines import (
    extract_company_from_headline,
    extract_company_from_news_headline,
    extract_funding_mention,
    extract_funding_round,
)


@dataclass(frozen=True)
class NormalizedBatch:
    candidates: list[CandidateRecord]
    skipped: int  # raw results that did not yield a valid candidate


class ActorSpec(Protocol):
    actor_id: ClassVar[str]

    def build_input(self, query: DiscoveryQuery) -> dict[str, Any]: ...

    def estimate_cost_usd(self, actor_input: Mapping[str, Any]) -> Decimal:
        """Worst-case cost of running the Actor with exactly this input."""
        ...

    def normalize(
        self, items: Sequence[Mapping[str, Any]], query: DiscoveryQuery
    ) -> NormalizedBatch: ...


# ------------------------------------------------------------------ apify/google-search-scraper
GOOGLE_SEARCH_VERIFIED_INPUT_KEYS = frozenset(
    {
        "afterDate",
        "aiModeSearch",
        "aiOverview",
        "beforeDate",
        "chatGptSearch",
        "copilotSearch",
        "countryCode",
        "fileTypes",
        "focusOnPaidAds",
        "forceExactMatch",
        "geminiSearch",
        "includeIcons",
        "includeUnfilteredResults",
        "languageCode",
        "leadsEnrichmentDepartments",
        "linkProspecting",
        "locationUule",
        "maxPagesPerQuery",
        "maximumLeadsEnrichmentRecords",
        "mobileResults",
        "perplexitySearch",
        "queries",
        "quickDateRange",
        "relatedToSite",
        "saveHtml",
        "saveHtmlToKeyValueStore",
        "searchLanguage",
        "site",
        "verifyLeadsEnrichmentEmails",
        "websiteContentScraper",
        "wordsInText",
        "wordsInTitle",
        "wordsInUrl",
    }
)

# Every paid add-on / storage side effect pinned OFF, even where the Actor default is already
# off (``saveHtmlToKeyValueStore`` defaults to TRUE in the Actor, so pinning matters).
_GOOGLE_SEARCH_SAFE_DEFAULTS: dict[str, Any] = {
    "maxPagesPerQuery": 1,
    "mobileResults": False,
    "includeUnfilteredResults": False,
    "saveHtml": False,
    "saveHtmlToKeyValueStore": False,
    "includeIcons": False,
    "focusOnPaidAds": False,
    "maximumLeadsEnrichmentRecords": 0,
    "verifyLeadsEnrichmentEmails": False,
    "aiOverview": {"scrapeFullAiOverview": False},
    "aiModeSearch": {"enableAiMode": False},
    "geminiSearch": {"enableGemini": False},
    "perplexitySearch": {
        "enablePerplexity": False,
        "returnImages": False,
        "returnRelatedQuestions": False,
    },
    "chatGptSearch": {"enableChatGpt": False},
    "copilotSearch": {"enableCopilot": False},
    "websiteContentScraper": {"enable": False},
}

# Harmless filters a caller may pass through ``DiscoveryQuery.params``.
_GOOGLE_SEARCH_ALLOWED_PARAMS = frozenset(
    {"quickDateRange", "afterDate", "beforeDate", "site", "languageCode", "searchLanguage"}
)


def _parse_date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


# Registry source that reads growth / intent headlines instead of funding headlines.
STARTUP_NEWS_SOURCE = "startup_news"


class GoogleSearchFundingNewsSpec:
    """Startup-news discovery via Google SERP. One dataset item = one results page.

    Serves two Source Actor Registry sources with the same Actor and the same pinned-safe
    input: ``funding_news`` (funding headlines) and ``startup_news`` (launch / expansion /
    hiring headlines). Only the default queries and the headline parser differ.
    """

    actor_id: ClassVar[str] = "apify/google-search-scraper"
    default_queries: ClassVar[tuple[str, ...]] = ("startup raises seed funding crore India",)
    default_news_queries: ClassVar[tuple[str, ...]] = ("Indian startup launches new platform",)
    # Pay-per-event prices, FREE tier = highest tier price (verified via Apify API 2026-09-28).
    # Paid add-ons are pinned off in the input, so only these two events can be charged.
    price_actor_start_usd: ClassVar[Decimal] = Decimal("0.001")
    price_search_page_usd: ClassVar[Decimal] = Decimal("0.0045")

    def estimate_cost_usd(self, actor_input: Mapping[str, Any]) -> Decimal:
        queries = [q for q in str(actor_input.get("queries", "")).splitlines() if q.strip()]
        pages = len(queries) * int(actor_input.get("maxPagesPerQuery") or 1)
        return self.price_actor_start_usd + pages * self.price_search_page_usd

    def build_input(self, query: DiscoveryQuery) -> dict[str, Any]:
        unknown = set(query.params) - _GOOGLE_SEARCH_ALLOWED_PARAMS
        if unknown:
            raise DomainValidationError(
                "unsupported discovery params for google-search-scraper",
                details={"unsupported": sorted(unknown)},
            )
        news = query.source == STARTUP_NEWS_SOURCE
        queries = query.keywords or list(
            self.default_news_queries if news else self.default_queries
        )
        actor_input: dict[str, Any] = {
            "queries": "\n".join(queries),
            "countryCode": query.country_codes[0].lower() if query.country_codes else "",
            **_GOOGLE_SEARCH_SAFE_DEFAULTS,
            **query.params,
        }
        unverified = set(actor_input) - GOOGLE_SEARCH_VERIFIED_INPUT_KEYS
        if unverified:  # pragma: no cover - guarded by construction; kept as a tripwire
            raise DomainValidationError(
                "unverified Actor input keys", details={"keys": sorted(unverified)}
            )
        return actor_input

    def normalize(
        self, items: Sequence[Mapping[str, Any]], query: DiscoveryQuery
    ) -> NormalizedBatch:
        candidates: list[CandidateRecord] = []
        skipped = 0
        for item in items:
            search_query = item.get("searchQuery") or {}
            organic = item.get("organicResults") or []
            for result in organic:
                candidate = self._to_candidate(result, search_query, query)
                if candidate is None:
                    skipped += 1
                else:
                    candidates.append(candidate)
        return NormalizedBatch(candidates=candidates, skipped=skipped)

    def _to_candidate(
        self,
        result: Mapping[str, Any],
        search_query: Mapping[str, Any],
        query: DiscoveryQuery,
    ) -> CandidateRecord | None:
        title = str(result.get("title") or "").strip()
        url = str(result.get("url") or "").strip()
        description = str(result.get("description") or "").strip()
        extract = (
            extract_company_from_news_headline
            if query.source == STARTUP_NEWS_SOURCE
            else extract_company_from_headline
        )
        company = extract(title) if title else None
        if company is None or not url:
            return None
        publisher = (urlsplit(url).hostname or "").lower().removeprefix("www.") or None
        try:
            return CandidateRecord(
                company_name=company,
                domain=None,  # the URL belongs to the publisher, not the company
                source_url=url,  # type: ignore[arg-type]  # validated by pydantic
                source_type=SourceType.NEWS,
                source_title=title[:500],
                source_platform=publisher,
                published_at=_parse_date(result.get("date")),
                raw={
                    "discovery_source": query.source,
                    "query_term": search_query.get("term"),
                    "serp_page": search_query.get("page"),
                    "position": result.get("position"),
                    "displayed_url": result.get("displayedUrl"),
                    "description": description[:1000],
                    "funding_mention": extract_funding_mention(f"{title} {description}"),
                    "funding_round": extract_funding_round(f"{title} {description}"),
                },
            )
        except ValidationError:
            return None


def actor_specs() -> dict[str, ActorSpec]:
    """Every registered spec by Actor id (social specs import this module, hence lazy)."""
    from app.providers.apify.social import (
        LinkedInJobsSpec,
        LinkedInPostSearchSpec,
        RedditSearchSpec,
        XPostSearchSpec,
    )

    specs: tuple[ActorSpec, ...] = (
        GoogleSearchFundingNewsSpec(),
        LinkedInPostSearchSpec(),
        LinkedInJobsSpec(),
        XPostSearchSpec(),
        RedditSearchSpec(),
    )
    return {spec.actor_id: spec for spec in specs}


ACTOR_SPECS: dict[str, ActorSpec] = {
    GoogleSearchFundingNewsSpec.actor_id: GoogleSearchFundingNewsSpec(),
}
