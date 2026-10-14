"""Signal-research domain rules (Phase 4) - pure, deterministic, vendor-neutral.

* Channel policy: which Agent-Reach channels exist, which are allowed, and why the others are
  disabled (recorded verbatim in reports).
* Relevance: a finding must name the company, otherwise it is dropped (no guessing).
* Classification: keyword rules map a finding's text to signal types (a finding can carry
  several types). Nothing is inferred beyond what the text says.
* Confidence: bounded in [0, 1]; channel base + explicit evidence bonuses, capped below 1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.domain.enums import ResearchChannel, SignalType

ALLOWED_CHANNELS: frozenset[ResearchChannel] = frozenset(ResearchChannel)

# Every channel Agent-Reach 1.5.0 offers that we do NOT use, with the reason (from
# `agent-reach doctor` + upstream docs, 2026-09-29, and the Phase 4 source policy).
DISABLED_CHANNELS: dict[str, str] = {
    "linkedin": "forbidden by Phase 4 policy (no LinkedIn fetching)",
    "instagram": "forbidden by policy; upstream needs a desktop browser session",
    "twitter_x": "upstream backend (twitter-cli/OpenCLI) requires exported login cookies",
    "reddit": "upstream backend requires login (no zero-config route)",
    "facebook": "upstream backend requires a desktop browser session",
    "xiaohongshu": "requires cookies/session; not relevant to the ICP",
    "xueqiu": "requires browser cookies; not relevant to the ICP",
    "boss_zhipin": "requires an isolated logged-in Chrome session",
    "xiaoyuzhou_podcast": "requires a Groq/OpenAI API key; not relevant to the ICP",
    "github": "gh CLI search requires `gh auth login` (account session)",
    "web_search_exa": "needs Node + mcporter + Exa MCP; general web search deferred with Brave",
    "web_reader_jina": "Google News item links are JS redirects (Jina returns 403); company "
    "websites are already covered by Phase 3",
    "v2ex": "public but Chinese developer community - not relevant to the ICP",
    "bilibili": "public but Chinese video platform - not relevant to the ICP",
}

# Research-time URL policy (narrower than the Phase 3 crawl policy, which also refuses to crawl
# YouTube/GitHub): findings pointing at these login-gated / forbidden platforms are dropped.
FORBIDDEN_RESEARCH_HOSTS = frozenset(
    {
        "linkedin.com",
        "instagram.com",
        "facebook.com",
        "fb.com",
        "x.com",
        "twitter.com",
        "threads.net",
        "tiktok.com",
        "reddit.com",
        "xiaohongshu.com",
        "xueqiu.com",
        "zhipin.com",
    }
)


def is_forbidden_research_url(url: str) -> bool:
    host = (urlsplit(url.strip()).hostname or "").lower().removeprefix("www.")
    return any(host == h or host.endswith("." + h) for h in FORBIDDEN_RESEARCH_HOSTS)


CHANNEL_BASE_CONFIDENCE: dict[ResearchChannel, float] = {
    ResearchChannel.NEWS_RSS: 0.55,  # published, attributable news
    ResearchChannel.YOUTUBE: 0.4,  # titles only; often interviews/marketing
}
MAX_CONFIDENCE = 0.95

_RULES: tuple[tuple[SignalType, re.Pattern[str]], ...] = (
    (
        SignalType.FUNDING,
        re.compile(
            r"\b(raises?|raised|funding|seed|pre-series|series [a-e]|investment|invests?|"
            r"investors?|crore|\bcr\b|million|valuation|backed)\b",
            re.I,
        ),
    ),
    (
        SignalType.AI_INTEREST,
        re.compile(
            r"\b(ai|artificial intelligence|machine learning|genai|llm|automation|automate[ds]?|"
            r"ai agents?|agentic|chatbot|data-driven|analytics)\b",
            re.I,
        ),
    ),
    (
        SignalType.HIRING,
        re.compile(r"\b(hiring|hires?|hired|recruit\w*|jobs?|openings?|talent|appoints?)\b", re.I),
    ),
    (
        SignalType.EXPANSION,
        re.compile(
            r"\b(expan\w*|launch\w*|enters?|new (city|market|office|region)|scal(e|es|ing) up|"
            r"footprint|rollout|roll out|deploy\w*|partners?|partnership|tie-up|mou|signs?)\b",
            re.I,
        ),
    ),
    (
        SignalType.PRODUCT_CUSTOMER,
        re.compile(
            r"\b(customers?|clients?|orders?|fleet operators?|contract|mou|partners? with|"
            r"deliver\w*|onboard\w*|financ\w*|leasing|pilot)\b",
            re.I,
        ),
    ),
    (
        SignalType.PAIN_POINT,
        re.compile(
            r"\b(delays?|shortage|bottlenecks?|challenges?|struggl\w*|manual|complaints?|"
            r"downtime|backlog|cost pressure|losses)\b",
            re.I,
        ),
    ),
)


def _alnum(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", text.lower())


def mentions_company(text: str, normalized_name: str) -> bool:
    """Whole-word(ish) match on the company name; compact match for multi-word names."""
    if not normalized_name.strip():
        return False
    words = normalized_name.split()
    if len(words) == 1:
        return (
            re.search(rf"(?<![0-9a-z]){re.escape(words[0])}(?![0-9a-z])", text.lower()) is not None
        )
    return _alnum(normalized_name) in _alnum(text)


def classify(text: str) -> list[SignalType]:
    return [signal_type for signal_type, pattern in _RULES if pattern.search(text)]


def context_terms_from(*texts: str | None, company_name: str | None = None) -> list[str]:
    """Distinctive words from the verified website (e.g. homepage title) used to disambiguate
    same-named entities: 'Drivn Transition | E-mobility | Commercial EV Leasing' ->
    ['transition', 'mobility', 'commercial', 'leasing']. Words of ``company_name`` are
    excluded - repeating the name adds no disambiguation (Phase 5 fix D4)."""
    stop = {"solution", "solutions", "private", "limited", "home", "official", "website", "india"}
    stop |= set(re.findall(r"[a-z0-9]+", (company_name or "").lower()))
    terms: list[str] = []
    for text in texts:
        for word in re.findall(r"[a-z]{4,}", (text or "").lower()):
            if word not in stop and word not in terms:
                terms.append(word)
    return terms[:8]


@dataclass(frozen=True)
class ScoredFinding:
    signal_types: list[SignalType]
    confidence: float
    evidence_notes: list[str]


def score(
    text: str,
    *,
    channel: ResearchChannel,
    normalized_name: str,
    domain: str | None,
    context_terms: list[str],
) -> ScoredFinding | None:
    """Return classified, bounded-confidence finding - or None when it is not about the company
    or carries no recognised signal."""
    if not mentions_company(text, normalized_name):
        return None
    types = classify(text)
    if not types:
        return None
    notes = [f"{channel.value} item names the company"]
    confidence = CHANNEL_BASE_CONFIDENCE[channel]
    lowered = text.lower()
    matched_context = [t for t in context_terms if t in lowered]
    if matched_context:
        confidence += 0.15
        notes.append("matches verified-website context: " + ", ".join(matched_context[:3]))
    if domain and domain.lower() in lowered:
        confidence += 0.15
        notes.append("mentions the verified domain")
    if len(types) > 1:
        confidence += 0.05
        notes.append("several signal cues")
    return ScoredFinding(types, round(min(confidence, MAX_CONFIDENCE), 3), notes)
