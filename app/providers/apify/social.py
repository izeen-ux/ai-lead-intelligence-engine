"""Actor specs for social / job-platform discovery sources (LinkedIn posts, LinkedIn jobs, X).

All three are third-party Apify Actors that read PUBLIC pages and need no login, cookie or
session from us - none is ever sent. Input keys and output fields were verified with one tiny
live run each on 2026-10-06 (5-10 items). Paid extras (profile enrichment, reactions,
comments) are pinned off, so a run costs exactly ``start + items x price per item``.

Only what discovery needs is kept: company name, public source URL, headline text, dates and -
for job posts - the company website / country / size the job board itself publishes. Personal
profile data of post authors is not stored.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar

from pydantic import ValidationError

from app.domain.enums import SourceType
from app.providers.apify.actors import NormalizedBatch
from app.providers.contracts import CandidateRecord, DiscoveryQuery
from app.utils.funding_headlines import (
    extract_company_from_headline,
    extract_funding_mention,
    extract_funding_round,
)

DEFAULT_ITEMS = 10
_LEAD_IN = re.compile(r"^[^A-Za-z0-9₹$@#]+|^\d{1,2}[.)]\s*")
_COMMA_CLAUSE = re.compile(r",\s[^,.]{3,90},\s")  # ", co-founded by A and B, " -> " "
_SPLIT = re.compile(r"[\n\r]+|(?<=[a-z0-9)])\.\s+(?=[A-Z@#])")
_MAX_PER_POST = 3


def _items(query: DiscoveryQuery) -> int:
    return max(1, min(query.max_items or DEFAULT_ITEMS, 100))


def companies_in_post(text: str) -> list[tuple[str, str]]:
    """(company, sentence) pairs named as funding recipients in a social post. A post can be
    a round-up, so up to three are returned; lines that do not clearly say "<Company> raises
    <funding>" are ignored (same conservative rules as news headlines)."""
    found: dict[str, tuple[str, str]] = {}
    for part in _SPLIT.split(unicodedata.normalize("NFKC", text or "")):
        line = _LEAD_IN.sub("", part.strip())
        line = _COMMA_CLAUSE.sub(" ", line).replace("#", "").replace("@", "")
        if not 12 <= len(line) <= 400:
            continue
        company = extract_company_from_headline(line)
        if company is not None and company.lower() not in found:
            found[company.lower()] = (company, line)
            if len(found) == _MAX_PER_POST:
                break
    return list(found.values())


def _when(value: Any) -> datetime | None:
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y")  # X: "Fri Oct 02 ..."
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True)
class SocialPost:
    """One public post in a source-independent shape (what the AI post reader gets)."""

    text: str
    url: str
    platform: str
    published: datetime | None


def post_candidate(
    company: str,
    sentence: str,
    post: SocialPost,
    query: DiscoveryQuery,
    *,
    extracted_by: str = "pattern",
) -> CandidateRecord | None:
    try:
        return CandidateRecord(
            company_name=company,
            source_url=post.url,  # type: ignore[arg-type]  # validated by pydantic
            source_type=SourceType.SOCIAL,
            source_title=sentence[:500],
            source_platform=post.platform,
            published_at=post.published,
            raw={
                "discovery_source": query.source,
                "query_term": query.keywords[0] if query.keywords else None,
                "description": post.text[:1000],
                "funding_mention": extract_funding_mention(sentence),
                "funding_round": extract_funding_round(sentence),
                "extracted_by": extracted_by,  # "pattern" | "llm" (AI post reader)
            },
        )
    except ValidationError:
        return None


def _post_candidates(
    text: str, url: str, platform: str, published: datetime | None, query: DiscoveryQuery
) -> list[CandidateRecord]:
    post = SocialPost(text, url, platform, published)
    found = (post_candidate(c, sentence, post, query) for c, sentence in companies_in_post(text))
    return [c for c in found if c is not None]


class LinkedInPostSearchSpec:
    """Public LinkedIn post search (funding / launch announcements). No cookies."""

    actor_id: ClassVar[str] = "harvestapi/linkedin-post-search"
    items_setting: ClassVar[str] = "discovery_social_max_items_per_run"
    default_queries: ClassVar[tuple[str, ...]] = ("startup raised seed round India",)
    price_start_usd: ClassVar[Decimal] = Decimal("0.00005")
    price_item_usd: ClassVar[Decimal] = Decimal("0.002")  # per post, FREE tier (highest)

    def build_input(self, query: DiscoveryQuery) -> dict[str, Any]:
        return {
            "searchQueries": (query.keywords or list(self.default_queries))[:1],
            "maxPosts": _items(query),
            "postedLimit": "3months",  # the 90-day trigger window
            "sortBy": "date",
            "profileScraperMode": "short",  # no paid profile enrichment
            "scrapeReactions": False,
            "scrapeComments": False,
        }

    def estimate_cost_usd(self, actor_input: Mapping[str, Any]) -> Decimal:
        return self.price_start_usd + int(actor_input.get("maxPosts") or 1) * self.price_item_usd

    def posts(self, items: Sequence[Mapping[str, Any]]) -> list[SocialPost]:
        return [
            SocialPost(
                str(item.get("content") or ""),
                str(item.get("linkedinUrl") or ""),
                "linkedin.com",
                _when((item.get("postedAt") or {}).get("date")),
            )
            for item in items
        ]

    def normalize(
        self, items: Sequence[Mapping[str, Any]], query: DiscoveryQuery
    ) -> NormalizedBatch:
        candidates: list[CandidateRecord] = []
        skipped = 0
        for item in items:
            posted = item.get("postedAt") or {}
            found = _post_candidates(
                str(item.get("content") or ""),
                str(item.get("linkedinUrl") or ""),
                "linkedin.com",
                _when(posted.get("date") or posted.get("timestamp")),
                query,
            )
            skipped += not found
            candidates += found
        return NormalizedBatch(candidates=candidates, skipped=skipped)


class XPostSearchSpec:
    """Public X (Twitter) post search. No cookies."""

    actor_id: ClassVar[str] = "apidojo/tweet-scraper"
    items_setting: ClassVar[str] = "discovery_social_max_items_per_run"
    default_queries: ClassVar[tuple[str, ...]] = ("startup raises seed funding India",)
    price_item_usd: ClassVar[Decimal] = Decimal("0.0004")  # per tweet

    def build_input(self, query: DiscoveryQuery) -> dict[str, Any]:
        return {
            "searchTerms": (query.keywords or list(self.default_queries))[:1],
            "maxItems": _items(query),
            "sort": "Latest",
            "tweetLanguage": "en",
        }

    def estimate_cost_usd(self, actor_input: Mapping[str, Any]) -> Decimal:
        return (int(actor_input.get("maxItems") or 1) + 1) * self.price_item_usd

    def posts(self, items: Sequence[Mapping[str, Any]]) -> list[SocialPost]:
        return [
            SocialPost(
                str(item.get("fullText") or item.get("text") or ""),
                str(item.get("url") or ""),
                "x.com",
                _when(item.get("createdAt")),
            )
            for item in items
            if not (item.get("isRetweet") or item.get("isReply"))
        ]

    def normalize(
        self, items: Sequence[Mapping[str, Any]], query: DiscoveryQuery
    ) -> NormalizedBatch:
        candidates: list[CandidateRecord] = []
        skipped = 0
        for item in items:
            if item.get("isRetweet") or item.get("isReply"):
                skipped += 1
                continue
            found = _post_candidates(
                str(item.get("fullText") or item.get("text") or ""),
                str(item.get("url") or ""),
                "x.com",
                _when(item.get("createdAt")),
                query,
            )
            skipped += not found
            candidates += found
        return NormalizedBatch(candidates=candidates, skipped=skipped)


class LinkedInJobsSpec:
    """Public LinkedIn job posts (hiring signal). The job board itself publishes the company
    website, country and size, so candidates arrive with a domain and can be size-filtered
    before any other credit is spent. No cookies."""

    actor_id: ClassVar[str] = "curious_coder/linkedin-jobs-scraper"
    items_setting: ClassVar[str] = "discovery_social_max_items_per_run"
    default_queries: ClassVar[tuple[str, ...]] = ("founding engineer startup",)
    price_start_usd: ClassVar[Decimal] = Decimal("0.00005")
    price_item_usd: ClassVar[Decimal] = Decimal("0.002")  # per job, FREE tier (highest)
    country: ClassVar[str] = "IN"
    max_employees: ClassVar[int] = 200  # larger companies are not early-stage startups

    def build_input(self, query: DiscoveryQuery) -> dict[str, Any]:
        return {
            "keywords": (query.keywords or list(self.default_queries))[0],
            "location": "India",
            "datePosted": "pastMonth",
            "limitPerSource": _items(query),
            "scrapeCompany": True,
        }

    def estimate_cost_usd(self, actor_input: Mapping[str, Any]) -> Decimal:
        jobs = int(actor_input.get("limitPerSource") or 1)
        return self.price_start_usd + jobs * self.price_item_usd

    def normalize(
        self, items: Sequence[Mapping[str, Any]], query: DiscoveryQuery
    ) -> NormalizedBatch:
        candidates: list[CandidateRecord] = []
        seen: set[str] = set()
        skipped = 0
        for item in items:
            candidate = self._candidate(item, query)
            if candidate is None or candidate.company_name.lower() in seen:
                skipped += 1
                continue
            seen.add(candidate.company_name.lower())
            candidates.append(candidate)
        return NormalizedBatch(candidates=candidates, skipped=skipped)

    def _candidate(self, item: Mapping[str, Any], query: DiscoveryQuery) -> CandidateRecord | None:
        name = str(item.get("companyName") or "").strip()
        url = str(item.get("link") or "").split("?")[0]
        address = item.get("companyAddress") or {}
        country = str(address.get("addressCountry") or "").upper()
        employees = item.get("companyEmployeesCount")
        too_big = isinstance(employees, int) and employees > self.max_employees
        if not name or not url or too_big or (country and country != self.country):
            return None
        title = str(item.get("title") or "").strip()
        try:
            return CandidateRecord(
                company_name=name,
                domain=str(item.get("companyWebsite") or "") or None,
                source_url=url,  # type: ignore[arg-type]  # validated by pydantic
                source_type=SourceType.JOB_BOARD,
                source_title=f"{name} is hiring: {title}"[:500],
                source_platform="linkedin.com",
                published_at=_when(item.get("postedAt")),
                raw={
                    "discovery_source": query.source,
                    "query_term": query.keywords[0] if query.keywords else None,
                    "description": str(item.get("companyDescription") or "")[:1000],
                    "employee_count": employees if isinstance(employees, int) else None,
                    "country": country or None,
                    "city": str(address.get("addressLocality") or "").strip() or None,
                    "industries": str(item.get("industries") or "")[:200] or None,
                    "funding_mention": None,
                    "funding_round": None,
                },
            )
        except (ValidationError, ValueError):
            return None


class RedditSearchSpec:
    """Public Reddit posts inside ONE startup subreddit per run. No cookies.

    A query is ``"<subreddit>:<keywords>"`` (e.g. ``"StartUpIndia:raised seed"``); searching
    all of Reddit returns almost only unrelated posts (verified 2026-10-06). ``maxPosts`` is
    the Actor's own limit: without it runs returned (and billed) 100 posts instead of the 10
    asked for - Reddit was the most expensive source for the fewest companies (2026-10-13)."""

    actor_id: ClassVar[str] = "fatihtahta/reddit-scraper-search-fast"
    items_setting: ClassVar[str] = "discovery_social_max_items_per_run"
    default_queries: ClassVar[tuple[str, ...]] = ("StartUpIndia:raised seed funding",)
    price_item_usd: ClassVar[Decimal] = Decimal("0.00149")  # per post, FREE tier (highest)

    def build_input(self, query: DiscoveryQuery) -> dict[str, Any]:
        raw = (query.keywords or list(self.default_queries))[0]
        subreddit, _, keywords = raw.partition(":")
        if not keywords.strip():  # no "sub:" prefix given - keep the search inside a startup sub
            subreddit, keywords = "StartUpIndia", raw
        return {
            "subredditName": subreddit.strip().removeprefix("r/"),
            "subredditKeywords": [keywords.strip()],
            "subredditSort": "new",
            "subredditTimeframe": "month",
            "scrapeComments": False,
            "maxPosts": _items(query),
            "_items": _items(query),  # not an Actor key: removed by ``actor_input``
        }

    def estimate_cost_usd(self, actor_input: Mapping[str, Any]) -> Decimal:
        return int(actor_input.get("_items") or DEFAULT_ITEMS) * self.price_item_usd

    def posts(self, items: Sequence[Mapping[str, Any]]) -> list[SocialPost]:
        return [
            SocialPost(
                f"{item.get('title') or ''}\n{item.get('body') or ''}",
                str(item.get("canonical_url") or item.get("url") or ""),
                "reddit.com",
                _when(item.get("created_utc")),
            )
            for item in items
            if not (item.get("over_18") or item.get("is_deleted_or_removed"))
        ]

    def normalize(
        self, items: Sequence[Mapping[str, Any]], query: DiscoveryQuery
    ) -> NormalizedBatch:
        candidates: list[CandidateRecord] = []
        skipped = 0
        for item in items:
            if item.get("over_18") or item.get("is_deleted_or_removed"):
                skipped += 1
                continue
            text = f"{item.get('title') or ''}\n{item.get('body') or ''}"
            found = _post_candidates(
                text,
                str(item.get("canonical_url") or item.get("url") or ""),
                "reddit.com",
                _when(item.get("created_utc")),
                query,
            )
            skipped += not found
            candidates += found
        return NormalizedBatch(candidates=candidates, skipped=skipped)
