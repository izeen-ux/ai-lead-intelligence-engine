"""Official-domain resolution scoring (pure, deterministic, auditable).

Given search hits for a company, score each candidate domain and decide:
  RESOLVED  - one clear winner above the confidence threshold (still verified later against the
              homepage before it is trusted),
  REVIEW    - plausible but ambiguous or below threshold -> a human decides, nothing is crawled,
  NOT_FOUND - no plausible candidate.

Conservative by design: the domain itself must correspond to the company name. A page title
that merely mentions the company (news article, directory listing) can never reach the
threshold on its own. Publisher hosts from the company's own discovery sources, social
platforms and directories are excluded outright.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from app.domain.enums import DomainResolutionStatus
from app.domain.website import host_of, is_non_official_host, is_social_url

_GENERIC_TOKENS = frozenset(
    {
        "ai",
        "the",
        "and",
        "labs",
        "lab",
        "tech",
        "technologies",
        "technology",
        "india",
        "solutions",
        "systems",
        "global",
        "group",
        "app",
        "hq",
        "inc",
    }
)
_DROPPABLE_SUBDOMAINS = frozenset(
    {"www", "app", "en", "in", "blog", "careers", "jobs", "docs", "help", "support", "shop"}
)
_BLOCKED_SUFFIXES = (".local", ".localhost", ".internal", ".test", ".invalid", ".lan")

MIN_MARGIN = 0.15
COUNTRY_NAMES = {"IN": "India"}


def _alnum(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", text.lower())


def site_domain(url: str) -> str | None:
    """Official-site domain for a result URL: host minus common app/locale subdomains."""
    host = host_of(url)
    if not host or "." not in host or host.endswith(_BLOCKED_SUFFIXES):
        return None
    labels = host.split(".")
    while len(labels) > 2 and labels[0] in _DROPPABLE_SUBDOMAINS:
        labels = labels[1:]
    return ".".join(labels)


def build_search_query(company_name: str, country: str | None = None) -> str:
    parts = [f'"{company_name}"', "official website"]
    if country and country in COUNTRY_NAMES:
        parts.append(COUNTRY_NAMES[country])
    return " ".join(parts)


@dataclass
class CandidateScore:
    domain: str
    url: str
    title: str | None
    position: int
    score: float = 0.0
    evidence: list[str] = field(default_factory=list)
    excluded_reason: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResolutionDecision:
    status: DomainResolutionStatus
    domain: str | None
    confidence: float
    reason: str
    candidates: list[CandidateScore]
    social_links: dict[str, str]


def score_candidates(
    normalized_name: str,
    hits: Sequence[Any],
    excluded_hosts: set[str] | frozenset[str] = frozenset(),
) -> tuple[list[CandidateScore], dict[str, str]]:
    compact = _alnum(normalized_name)
    tokens = [t for t in normalized_name.split() if t not in _GENERIC_TOKENS and len(t) >= 3]
    primary = tokens[0] if tokens else compact
    domains_seen = Counter(site_domain(h.url) for h in hits)
    scored: list[CandidateScore] = []
    social: dict[str, str] = {}

    for hit in hits:
        domain = site_domain(hit.url)
        if domain is None:
            continue
        candidate = CandidateScore(
            domain=domain, url=hit.url, title=hit.title, position=hit.position
        )
        if is_social_url(hit.url):
            social.setdefault(host_of(hit.url), hit.url)
            candidate.excluded_reason = "social platform (metadata only)"
        elif is_non_official_host(domain):
            candidate.excluded_reason = "directory/news/app-store host"
        elif domain in excluded_hosts or host_of(hit.url) in excluded_hosts:
            candidate.excluded_reason = "publisher of the company's discovery source"
        if candidate.excluded_reason:
            scored.append(candidate)
            continue

        label = _alnum(domain.split(".")[0])
        if label and label == compact:
            candidate.score += 0.6
            candidate.evidence.append("domain label equals company name")
        elif label and label == _alnum(primary):
            candidate.score += 0.5
            candidate.evidence.append("domain label equals primary name token")
        elif len(compact) >= 4 and label and (compact in label or label in compact):
            candidate.score += 0.35
            candidate.evidence.append("domain contains company name")
        if compact and compact in _alnum(hit.title or ""):
            candidate.score += 0.25
            candidate.evidence.append("result title names the company")
        if compact and compact in _alnum(hit.description or ""):
            candidate.score += 0.1
            candidate.evidence.append("result description names the company")
        if hit.position == 1:
            candidate.score += 0.1
            candidate.evidence.append("top search result")
        elif hit.position == 2:
            candidate.score += 0.05
            candidate.evidence.append("second search result")
        if domains_seen[domain] > 1:
            candidate.score += 0.05
            candidate.evidence.append("domain appears in several results")
        candidate.score = round(min(candidate.score, 1.0), 3)
        scored.append(candidate)
    return scored, social


def decide(
    normalized_name: str,
    hits: Sequence[Any],
    *,
    min_confidence: float,
    excluded_hosts: set[str] | frozenset[str] = frozenset(),
) -> ResolutionDecision:
    scored, social = score_candidates(normalized_name, hits, excluded_hosts)
    eligible: dict[str, CandidateScore] = {}
    for c in scored:
        if c.excluded_reason is None and (
            c.domain not in eligible or c.score > eligible[c.domain].score
        ):
            eligible[c.domain] = c
    ranked = sorted(eligible.values(), key=lambda c: (-c.score, c.position))
    if not ranked or ranked[0].score <= 0:
        return ResolutionDecision(
            DomainResolutionStatus.NOT_FOUND,
            None,
            0.0,
            "no plausible official site",
            scored,
            social,
        )
    top = ranked[0]
    runner_up = ranked[1].score if len(ranked) > 1 else 0.0
    if top.score < min_confidence:
        return ResolutionDecision(
            DomainResolutionStatus.REVIEW,
            top.domain,
            top.score,
            f"best candidate below confidence threshold {min_confidence}",
            scored,
            social,
        )
    if top.score - runner_up < MIN_MARGIN:
        return ResolutionDecision(
            DomainResolutionStatus.REVIEW,
            top.domain,
            top.score,
            "ambiguous: another domain scored almost as high",
            scored,
            social,
        )
    return ResolutionDecision(
        DomainResolutionStatus.RESOLVED,
        top.domain,
        top.score,
        "; ".join(top.evidence),
        scored,
        social,
    )
