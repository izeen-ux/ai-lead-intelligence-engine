"""Source Actor Registry (Phase 6) - which discovery sources exist and which may run.

Every future source-specific Apify Actor is declared here with its policy state, auth needs,
allowed fields and limits. A source can run only when ALL hold:
  1. it is declared here;
  2. its ``policy_state`` is APPROVED (terms/policies, Actor behaviour, data collected and
     intended-use restrictions were reviewed and recorded in ``policy_notes``);
  3. it needs NO authentication (no cookies, logins or session tokens - ever);
  4. it has a parser (normalizer) implemented;
  5. it is enabled via ``DISCOVERY_SOURCES_ENABLED``.
Declaring a source does NOT activate it. No credential extraction, cookie use, CAPTCHA bypass,
access-control circumvention or stealth evasion is supported by design.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.errors import SourceNotRunnableError
from app.domain.enums import SourcePolicyState


@dataclass(frozen=True)
class SourceDefinition:
    source_id: str
    actor_id: str | None
    description: str
    policy_state: SourcePolicyState
    auth_required: bool
    allowed_fields: tuple[str, ...]
    max_items_per_run: int
    rate_limit_per_hour: int
    max_cost_usd_per_run: str
    parser: str | None  # normalizer id in app/providers/apify/actors.py; None = not built
    policy_notes: str = ""


_PUBLIC_FIELDS = ("company_name", "source_url", "source_title", "published_at", "platform")

SOURCES: dict[str, SourceDefinition] = {
    s.source_id: s
    for s in (
        SourceDefinition(
            "funding_news",
            "apify/google-search-scraper",
            "Google Search results for Indian startup funding news (Phase 2)",
            SourcePolicyState.APPROVED,
            False,
            _PUBLIC_FIELDS,
            1,
            60,
            "0.50",
            "google_search_funding_news",
            "Public search results; paid add-ons pinned off; verified 2026-09-28.",
        ),
        SourceDefinition(
            "startup_news",
            "apify/google-search-scraper",
            "Google Search results for Indian startup launch / expansion / hiring news",
            SourcePolicyState.APPROVED,
            False,
            _PUBLIC_FIELDS,
            1,
            60,
            "0.50",
            "google_search_startup_news",
            "Same public search Actor and pinned-safe input as funding_news; no login, no "
            "cookies; reviewed 2026-10-05. Off until listed in DISCOVERY_SOURCES_ENABLED.",
        ),
        SourceDefinition(
            "linkedin_posts",
            "harvestapi/linkedin-post-search",
            "Public LinkedIn posts about funding / launches (third-party Actor, no cookies)",
            SourcePolicyState.APPROVED,
            False,
            _PUBLIC_FIELDS,
            10,
            30,
            "0.50",
            "linkedin_post_search",
            "Owner decision 2026-10-06: public posts via a third-party Apify Actor; we send no "
            "login, cookie or session. LinkedIn's terms prohibit scraping - the risk is "
            "accepted by the owner. Author profile data is not stored.",
        ),
        SourceDefinition(
            "linkedin_jobs",
            "curious_coder/linkedin-jobs-scraper",
            "Public LinkedIn job posts of small Indian companies (hiring signal, no cookies)",
            SourcePolicyState.APPROVED,
            False,
            (*_PUBLIC_FIELDS, "company_website", "employee_count", "country"),
            10,
            30,
            "0.50",
            "linkedin_jobs",
            "Owner decision 2026-10-06: public job pages via a third-party Apify Actor; no "
            "login, cookie or session. Same terms-of-service note as linkedin_posts.",
        ),
        SourceDefinition(
            "x_posts",
            "apidojo/tweet-scraper",
            "Public X (Twitter) posts about funding (third-party Actor, no cookies)",
            SourcePolicyState.APPROVED,
            False,
            _PUBLIC_FIELDS,
            10,
            30,
            "0.50",
            "x_post_search",
            "Owner decision 2026-10-06: public posts via a third-party Apify Actor; no login, "
            "cookie or session. X's terms restrict scraping - risk accepted by the owner.",
        ),
        SourceDefinition(
            "linkedin",
            None,
            "LinkedIn company/people pages",
            SourcePolicyState.BLOCKED,
            True,
            (),
            0,
            0,
            "0",
            None,
            "Login-gated; ToS prohibits scraping; Phase 4 policy forbids LinkedIn.",
        ),
        SourceDefinition(
            "x_twitter",
            None,
            "X / Twitter posts",
            SourcePolicyState.BLOCKED,
            True,
            (),
            0,
            0,
            "0",
            None,
            "Actors require exported login cookies/sessions.",
        ),
        SourceDefinition(
            "reddit",
            "fatihtahta/reddit-scraper-search-fast",
            "Public Reddit posts in Indian startup subreddits (third-party Actor, no cookies)",
            SourcePolicyState.APPROVED,
            False,
            _PUBLIC_FIELDS,
            10,
            30,
            "0.50",
            "reddit_search",
            "Owner decision 2026-10-06: public posts via a third-party Apify Actor; no login, "
            "cookie or session. Low yield for funding leads - searches stay inside the "
            "configured subreddits. Post author data is not stored.",
        ),
        SourceDefinition(
            "youtube",
            None,
            "YouTube video metadata via Apify",
            SourcePolicyState.PENDING_REVIEW,
            False,
            (),
            0,
            0,
            "0",
            None,
            "Covered today by Agent-Reach (yt-dlp metadata); Actor not reviewed.",
        ),
        SourceDefinition(
            "job_boards",
            None,
            "Public job postings (hiring signals)",
            SourcePolicyState.PENDING_REVIEW,
            False,
            (),
            0,
            0,
            "0",
            None,
            "Per-board terms differ; review each board before activation.",
        ),
        SourceDefinition(
            "startup_databases",
            None,
            "Startup/funding databases",
            SourcePolicyState.PENDING_REVIEW,
            False,
            (),
            0,
            0,
            "0",
            None,
            "Most require accounts or licences; review terms and licensing.",
        ),
        SourceDefinition(
            "directories",
            None,
            "Public business directories",
            SourcePolicyState.PENDING_REVIEW,
            False,
            (),
            0,
            0,
            "0",
            None,
            "Review robots/terms per directory.",
        ),
    )
}


@dataclass
class SourceRegistry:
    enabled: frozenset[str] = frozenset({"funding_news"})
    sources: dict[str, SourceDefinition] = field(default_factory=lambda: dict(SOURCES))

    @classmethod
    def from_setting(cls, value: str) -> SourceRegistry:
        return cls(enabled=frozenset(s.strip() for s in value.split(",") if s.strip()))

    def blocked_reason(self, source_id: str) -> str | None:
        src = self.sources.get(source_id)
        if src is None:
            return "unknown source (not declared in the Source Actor Registry)"
        if src.policy_state is not SourcePolicyState.APPROVED:
            return f"policy state is {src.policy_state.value}: {src.policy_notes}"
        if src.auth_required:
            return "source requires authentication - never supported"
        if src.parser is None or src.actor_id is None:
            return "no Actor/parser implemented"
        if source_id not in self.enabled:
            return "source is disabled (DISCOVERY_SOURCES_ENABLED)"
        return None

    def require_runnable(self, source_id: str) -> SourceDefinition:
        reason = self.blocked_reason(source_id)
        if reason is not None:
            raise SourceNotRunnableError(
                f"discovery source {source_id!r} cannot run: {reason}",
                details={"source_id": source_id},
            )
        return self.sources[source_id]

    def runnable(self) -> list[str]:
        return sorted(s for s in self.sources if self.blocked_reason(s) is None)
