"""Heuristic parsing of funding-news headlines ("Acme raises Rs 3 Cr in seed round").

Pure functions, no I/O. Used to turn search/news results into candidate companies. The output
is deliberately conservative: when a headline does not clearly name a company that raised
money, we return ``None`` and the result is skipped rather than guessed. Accuracy is reviewed
again by website intelligence (Phase 3) and the qualification gate (Phase 5).
"""

from __future__ import annotations

import re

_VERBS = (
    r"(?:raises?|raised|secures?|secured|bags?|bagged|gets?|got|closes?|closed|lands?|landed|"
    r"nets?|netted|receives?|received|mops?\s+up|mopped\s+up|picks?\s+up|picked\s+up|snags?|"
    r"snagged|scores?|scored|attracts?|attracted)"
)
# Growth / intent verbs for non-funding startup news ("Acme launches ...", "Acme expands to ...").
_NEWS_VERBS = (
    r"(?:launch(?:es|ed)?|unveil(?:s|ed)?|introduc(?:es|ed)|rolls?\s+out|rolled\s+out|"
    r"expand(?:s|ed)?|enters?|entered|opens?|opened|hir(?:es|ed|ing)|appoint(?:s|ed)?|"
    r"partners?|partnered|ties?\s+up|tied\s+up|cross(?:es|ed)|clock(?:s|ed)?|acquir(?:es|ed))"
)
_NEWS_HEADLINE = re.compile(rf"^(?P<subject>.+?)\s+{_NEWS_VERBS}\s+(?P<rest>.+)$", re.IGNORECASE)
_HEADLINE = re.compile(rf"^(?P<subject>.+?)\s+{_VERBS}\s+(?P<rest>.+)$", re.IGNORECASE)
_FUNDING_CONTEXT = re.compile(
    r"(funding|seed|pre-seed|series\s+[a-e]|round|investment|capital|crore|\bcr\b|lakh|"
    r"million|\bmn\b|\$|₹|\brs\.?|\binr\b|\busd\b)",
    re.IGNORECASE,
)
_LEADING_TAG = re.compile(
    r"^\s*(?:\[[^\]]*\]\s*|(?:exclusive|breaking|funding\s+alert|funding\s+news|update|report|"
    r"funding\s+roundup)\s*[:|\-\u2013\u2014]\s*)",
    re.IGNORECASE,
)
_DESCRIPTORS = frozenset(
    {
        "startup",
        "start-up",
        "platform",
        "brand",
        "company",
        "firm",
        "maker",
        "app",
        "fintech",
        "edtech",
        "healthtech",
        "agritech",
        "saas",
        "d2c",
        "provider",
        "marketplace",
        "unicorn",
    }
)
_REJECT_FIRST_WORD = re.compile(
    r"^(how|why|what|when|who|where|top|best|these|this|those|indian|india|startups?|"
    r"investors?|founders?|funding|weekly|here|seed|pre-seed|angel|venture|series|"
    r"looking|planning|meet|inside|ecosystem|government|govt|we|our|your|the|an?)\b",
    re.IGNORECASE,
)
# Words that end a headline subject without being part of the name ("Acme has raised ...",
# "Acme just launched ...", "Acme to launch ...").
_TRAILING_FILLER = frozenset(
    {
        "has", "have", "had", "is", "was", "are", "will", "to", "just", "also", "now", "today",
        "recently", "reportedly", "officially", "quietly", "finally", "set", "plans", "and",
    }
)  # fmt: skip
_GENERIC_PLURALS = frozenset(
    {"startups", "companies", "firms", "founders", "investors", "brands", "unicorns", "funds"}
)
_SUBJECT_LABEL = frozenset({"funding", "news", "update", "alert", "exclusive", "report"})
_MONEY = re.compile(
    r"(?:₹|rs\.?|inr|us\$|\$|usd)\s?\d[\d,]*(?:\.\d+)?\s?"
    r"(?:crore|cr|lakhs?|million|mn|m|billion|bn|k)?\b"
    r"|\b\d[\d,]*(?:\.\d+)?\s?(?:crore|cr|lakhs?|million|mn|billion|bn)\b",
    re.IGNORECASE,
)
_ROUND = re.compile(
    r"\b(pre-seed|seed|angel|pre-series\s+[a-e]|series\s+[a-e]|bridge)\b", re.IGNORECASE
)
_MAX_NAME_WORDS = 6
_EDGE_SYMBOLS = re.compile(r"^[^\w(]+|[^\w).]+$")
# Characters trimmed from both ends of an extracted name: space, quotes (straight and curly),
# colon, comma, pipe, hyphen, en dash, em dash.
_NAME_STRIP_CHARS = " \"'\u201c\u201d\u2018\u2019:,|-\u2013\u2014"


def _strip_descriptors(subject: str) -> str:
    # "Bengaluru-based fintech startup Acme Pay" -> "Acme Pay"
    if "-based " in subject.lower():
        subject = subject[subject.lower().rindex("-based ") + len("-based ") :]
    words = subject.split()
    last_descriptor = max(
        (i for i, w in enumerate(words) if w.lower().strip(",") in _DESCRIPTORS), default=-1
    )
    if 0 <= last_descriptor < len(words) - 1:
        words = words[last_descriptor + 1 :]
    return " ".join(words)


def extract_company_from_headline(title: str) -> str | None:
    """Return the company named as the funding recipient, or ``None`` if unclear."""
    text = _LEADING_TAG.sub("", title.strip())
    match = _HEADLINE.match(text)
    if not match or not _FUNDING_CONTEXT.search(match.group("rest")):
        return None
    return _clean_name(match.group("subject"))


def extract_company_from_news_headline(title: str) -> str | None:
    """Company named as the actor of a launch / expansion / hiring headline, or ``None``.

    Same conservative name rules as funding headlines; a funding headline also counts.
    """
    funded = extract_company_from_headline(title)
    if funded is not None:
        return funded
    match = _NEWS_HEADLINE.match(_LEADING_TAG.sub("", title.strip()))
    return _clean_name(match.group("subject")) if match else None


def _before_label(subject: str) -> str:
    """ "Firi Funding: Beauty Quick Commerce Startup" -> "Firi": when the text after a colon
    is only a description (it ends with a descriptor word), the name is in front of it."""
    left, sep, right = subject.partition(":")
    words = right.split()
    if not sep or not words or words[-1].lower().strip(",") not in _DESCRIPTORS:
        return subject
    kept = left.split()
    while kept and kept[-1].lower() in _SUBJECT_LABEL:
        kept.pop()
    return " ".join(kept)


def _clean_name(subject: str) -> str | None:
    words = _strip_descriptors(_before_label(subject)).split()
    while words and words[-1].lower().strip(",") in _TRAILING_FILLER:
        words.pop()
    name = " ".join(words).strip(_NAME_STRIP_CHARS)
    name = _EDGE_SYMBOLS.sub("", name)  # emoji / decorative symbols at either end
    if not name or _REJECT_FIRST_WORD.match(name) or name[0].isdigit():
        return None
    if len(name.split()) > _MAX_NAME_WORDS or not any(ch.isupper() for ch in name):
        return None
    if name.split()[-1].lower() in _GENERIC_PLURALS:  # "European defense startups"
        return None
    return name


def plausible_company_name(name: str) -> bool:
    """A stored candidate name that survives the headline name rules unchanged. False for
    fragments such as "Factrika has", "Looking to", "Ecosystem" or an all-lowercase phrase."""
    return _clean_name(name) == name.strip()


def extract_funding_mention(text: str) -> str | None:
    """First money amount in the text, verbatim (e.g. ``"Rs 3 Cr"``). Parsing into INR is
    deferred to the qualification phase."""
    match = _MONEY.search(text)
    return match.group(0).strip() if match else None


def extract_funding_round(text: str) -> str | None:
    match = _ROUND.search(text)
    return match.group(1).lower() if match else None
