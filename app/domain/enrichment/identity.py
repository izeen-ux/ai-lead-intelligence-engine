"""Person / founder identity resolution for a QUALIFIED company - never invents identities.

Sources (all attributable, provenance kept):
  * the company's OWN crawled pages on the verified domain (Phase 3 ``website_pages``):
    - headings like ``### Manav Bansal - CEO`` on the team/about page;
    - sentences ``<Name> is the <title> of <Company>`` where <Company> is THIS company
      (a bio saying "Co-Founder at Moki" is about another company and is ignored);
  * optional provider discovery on the verified domain (Hunter Domain Search, executives).

Rules:
  * only configured senior decision-maker roles are eligible (``ENRICHMENT_ROLE_PRIORITY``);
    board/independent/advisory/former/investor titles are never eligible;
  * a name needs first + last name (no initials-only, no single names);
  * two different senior people sharing a first name and last initial are AMBIGUOUS - neither
    is enriched;
  * the best ``max_persons_per_company`` are selected by role rank, then number of independent
    sources, then name (deterministic); the rest are recorded as skipped.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from uuid import UUID

from app.domain.enrichment.config import EnrichmentConfig
from app.providers.contracts import DiscoveredPerson

_HONORIFICS = {"mr", "mrs", "ms", "dr", "shri", "smt", "prof", "sir"}
_EXCLUDED_TITLE = re.compile(
    r"\b(independent|non-executive|board member|advisor|adviser|advisory|investor|former|ex-|"
    r"mentor|intern)\b",
    re.I,
)
_ROLE_WORDS = re.compile(
    r"\b(founder|co-?founder|ceo|cto|cfo|coo|cbo|chief|president|owner|director|head|"
    r"managing|partner|vp|vice president)\b",
    re.I,
)
_WORD = r"[A-Z][a-zA-Z'\u2019]+"
_NAME = rf"(?:(?:Mr|Mrs|Ms|Dr|Shri|Smt|Prof)\.? +)?{_WORD}(?: +{_WORD}){{1,3}}"  # spaces only
_HEADING = re.compile(
    rf"^#{{1,6}}\s*(?P<name>{_NAME})\s*[-\u2013\u2014|:,]\s*(?P<title>[^#\n]{{2,80}})$", re.M
)
_SENTENCE = re.compile(
    rf"(?P<name>{_NAME})\s+is\s+(?:currently\s+)?(?:the|a|an)\s+(?P<title>[^.;\n]{{3,90}}?)\s+"
    rf"(?:of|at)\s+(?P<org>[A-Z][\w&.'\u2019\- ]{{1,60}}?)(?=[,.;:]|\s+-|\s+which|\s+where|\s*$)",
    re.M,
)


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def normalize_name(name: str) -> str:
    """'Mr. Manav  Bansal' -> 'manav bansal' (stable key part)."""
    words = re.findall(r"[a-z]+", _fold(name).lower())
    return " ".join(w for w in words if w not in _HONORIFICS)


def display_name(name: str) -> str:
    words = [w for w in re.split(r"\s+", name.strip()) if w.rstrip(".").lower() not in _HONORIFICS]
    return " ".join(words)


def canonical_title(title: str) -> str:
    t = title.strip().lower()
    t = re.sub(r"\bco[\s-]?founder\b", "co-founder", t)
    t = re.sub(r"\bchief executive officer\b", "chief executive ceo", t)
    return re.sub(r"\s+", " ", t)


def role_rank(title: str | None, priority: Sequence[str]) -> tuple[int, str, int] | None:
    """(best rank, matched role, number of matched senior roles) or None when the title is not
    an eligible decision-maker role. 'CEO and Co-Founder' outranks a plain 'Co-Founder'."""
    if not title or _EXCLUDED_TITLE.search(title):
        return None
    t = canonical_title(title)
    matches = [
        (rank, role)
        for rank, role in enumerate(priority)
        if re.search(rf"(?<![a-z]){re.escape(role)}(?![a-z])", t)
    ]
    if not matches:
        return None
    rank, role = matches[0]
    return rank, role, len(matches)


def person_key(domain: str, name: str) -> str:
    return f"{domain.lower()}|{normalize_name(name)}"


def cache_key(domain: str, name: str) -> str:
    return hashlib.sha256(person_key(domain, name).encode()).hexdigest()[:40]


@dataclass
class PersonCandidate:
    full_name: str
    title: str | None
    sources: list[str] = field(default_factory=list)  # URLs / "hunter_domain_search:<domain>"
    source_ids: list[UUID] = field(default_factory=list)  # e.g. website_pages ids
    email_hint: str | None = None
    hint_confidence: float | None = None
    evidence: list[str] = field(default_factory=list)
    linkedin_url: str | None = None  # only from public search-result metadata

    @property
    def key(self) -> str:
        return normalize_name(self.full_name)

    @property
    def tokens(self) -> list[str]:
        return self.key.split()


@dataclass(frozen=True)
class PageText:
    url: str
    markdown: str
    page_id: UUID | None = None


def _org_is(org: str, company_name: str) -> bool:
    a, b = normalize_name(org), normalize_name(company_name)
    return bool(b) and (a == b or a.startswith(b + " ") or a.split()[:1] == b.split()[:1])


def extract_people(pages: Iterable[PageText], company_name: str) -> list[PersonCandidate]:
    """Named people with a role, from the company's own pages. Deterministic."""
    found: dict[str, PersonCandidate] = {}

    def add(name: str, title: str, page: PageText, evidence: str) -> None:
        name = display_name(name)
        cand = PersonCandidate(
            full_name=name, title=title.strip(" -\u2013\u2014|,:"), sources=[page.url]
        )
        if page.page_id is not None:
            cand.source_ids.append(page.page_id)
        cand.evidence.append(evidence[:200])
        existing = found.get(cand.key)
        if existing is None:
            found[cand.key] = cand
            return
        if page.url not in existing.sources:
            existing.sources.append(page.url)
        existing.source_ids.extend(i for i in cand.source_ids if i not in existing.source_ids)
        existing.evidence.append(evidence[:200])
        if existing.title and cand.title and cand.title.lower() not in existing.title.lower():
            existing.title = f"{existing.title}; {cand.title}"

    for page in pages:
        text = page.markdown or ""
        for m in _HEADING.finditer(text):
            if _ROLE_WORDS.search(m.group("title")):
                add(m.group("name"), m.group("title"), page, m.group(0))
        for m in _SENTENCE.finditer(text):
            if _ROLE_WORDS.search(m.group("title")) and _org_is(m.group("org"), company_name):
                add(m.group("name"), m.group("title"), page, m.group(0))
    return list(found.values())


_LINKEDIN_PROFILE = re.compile(
    r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9\-_%]{3,100}", re.I
)
_PROFILE_WINDOW = 400  # characters after the person's name in which their link must appear


def linkedin_from_pages(full_name: str, pages: Iterable[PageText]) -> tuple[str, str] | None:
    """(profile URL, page URL) when the company's OWN page links a ``linkedin.com/in/...``
    profile right after this person's name - before any other heading. Public link metadata
    only: the LinkedIn page itself is never fetched. None when there is no such link."""
    words = [re.escape(w) for w in display_name(full_name).split()]
    if len(words) < 2:
        return None
    name = re.compile(r"\s+".join(words), re.I)
    for page in pages:
        text = page.markdown or ""
        for m in name.finditer(text):
            window = text[m.end() : m.end() + _PROFILE_WINDOW]
            heading = re.search(r"^#{1,6}\s", window, re.M)
            if heading is not None:
                window = window[: heading.start()]
            link = _LINKEDIN_PROFILE.search(window)
            if link is not None:
                return link.group(0).rstrip("/"), page.url
    return None


# Public web-search results (title + snippet only - no page is fetched). Typical shapes:
#   "Asha Rao - Founder & CEO - Acme Robotics | LinkedIn"
#   "Acme Robotics founder Asha Rao on ..." / "Asha Rao, co-founder and CEO of Acme Robotics"
_TITLE_WORDS = (
    r"(?i:(?:co[\s-]?)?founder(?:\s*(?:&|and)\s*(?:ceo|cto|coo|director))?|ceo|"
    r"chief executive(?: officer)?|managing director)"
)
_RESULT_TITLE = re.compile(
    rf"^(?P<name>{_NAME})\s*[-\u2013\u2014|]\s*(?P<title>[^-\u2013\u2014|]{{2,60}}?)\s*"
    rf"(?:[-\u2013\u2014|@]|\bat\b)\s*(?P<org>[^|\u2013\u2014]{{2,60}})"
)
_NAME_THEN_ROLE = re.compile(
    rf"(?P<name>{_NAME}),?\s+(?:is\s+)?(?:(?:the|a)\s+)?(?P<title>{_TITLE_WORDS})\s+(?:of|at)\s+"
    rf"(?P<org>[A-Z][\w&.'\u2019\- ]{{1,60}})"
)
_ROLE_THEN_NAME = re.compile(
    rf"(?P<org>[A-Z][\w&.'\u2019\-]*(?: [A-Z][\w&.'\u2019\-]*){{0,3}})(?:'s)?\s+"
    rf"(?P<title>{_TITLE_WORDS})\s+(?P<name>{_NAME})"
)


def candidates_from_web_search(
    hits: Iterable[tuple[str, str]], company_name: str
) -> list[PersonCandidate]:
    """People named as founder / CEO of THIS company in public search results.

    ``hits`` = (result URL, title + snippet). A person is kept only when the same result names
    the company next to the role, so a namesake or another firm's founder is ignored. A
    ``linkedin.com/in/...`` result URL is kept as that person's profile link - it comes from
    the search engine's result list; the LinkedIn page itself is never requested.
    """
    found: dict[str, PersonCandidate] = {}
    for url, text in hits:
        for pattern in (_RESULT_TITLE, _NAME_THEN_ROLE, _ROLE_THEN_NAME):
            for m in pattern.finditer(text):
                title = m.group("title").strip(" -|,")
                org, wanted = normalize_name(m.group("org")), normalize_name(company_name)
                named = _org_is(m.group("org"), company_name) or org.endswith(f" {wanted}")
                if not _ROLE_WORDS.search(title) or not wanted or not named:
                    continue
                name = display_name(m.group("name"))
                cand = found.setdefault(
                    normalize_name(name), PersonCandidate(full_name=name, title=title)
                )
                if url not in cand.sources:
                    cand.sources.append(url)
                    cand.evidence.append(f"web search result: {m.group(0)[:160]}")
                if cand.linkedin_url is None and _LINKEDIN_PROFILE.match(url):
                    cand.linkedin_url = url.split("?")[0].rstrip("/")
    return list(found.values())


def candidates_from_search(
    people: Iterable[DiscoveredPerson], domain: str
) -> list[PersonCandidate]:
    out = []
    for p in people:
        if not (p.first_name and p.last_name):
            continue
        out.append(
            PersonCandidate(
                full_name=f"{p.first_name} {p.last_name}",
                title=p.position,
                sources=[f"hunter_domain_search:{domain}"],
                email_hint=p.email,
                hint_confidence=p.confidence,
                evidence=[f"Hunter Domain Search: {p.position or 'no position'}"],
            )
        )
    return out


def merge_candidates(*groups: Iterable[PersonCandidate]) -> list[PersonCandidate]:
    merged: dict[str, PersonCandidate] = {}
    for group in groups:
        for c in group:
            existing = merged.get(c.key)
            if existing is None:
                merged[c.key] = c
                continue
            existing.sources.extend(s for s in c.sources if s not in existing.sources)
            existing.source_ids.extend(i for i in c.source_ids if i not in existing.source_ids)
            existing.evidence.extend(c.evidence)
            existing.email_hint = existing.email_hint or c.email_hint
            existing.hint_confidence = existing.hint_confidence or c.hint_confidence
            if not existing.title:
                existing.title = c.title
    return list(merged.values())


@dataclass(frozen=True)
class PersonIdentity:
    company_id: UUID
    company_name: str
    domain: str
    full_name: str
    first_name: str
    last_name: str
    title: str | None
    role: str
    role_rank: int
    sources: tuple[str, ...]
    source_ids: tuple[UUID, ...]
    email_hint: str | None = None

    @property
    def person_key(self) -> str:
        return person_key(self.domain, self.full_name)

    @property
    def cache_key(self) -> str:
        return cache_key(self.domain, self.full_name)

    def as_record(self) -> dict[str, object]:
        return {
            "full_name": self.full_name,
            "title": self.title,
            "role": self.role,
            "role_rank": self.role_rank,
            "sources": list(self.sources),
            "source_ids": [str(i) for i in self.source_ids],
        }


@dataclass
class IdentityResolution:
    selected: list[PersonIdentity]
    skipped: list[tuple[str, str]]  # (name, reason)
    ambiguous: list[str]

    @property
    def status(self) -> str:
        if self.selected:
            return "ok"
        return "ambiguous" if self.ambiguous else "no_person"


def resolve_identities(
    candidates: Sequence[PersonCandidate],
    *,
    company_id: UUID,
    company_name: str,
    domain: str,
    config: EnrichmentConfig,
) -> IdentityResolution:
    skipped: list[tuple[str, str]] = []
    eligible: list[tuple[int, str, int, PersonCandidate]] = []
    for c in candidates:
        ranked = role_rank(c.title, config.role_priority)
        if ranked is None:
            skipped.append((c.full_name, f"title not an eligible decision-maker role ({c.title})"))
            continue
        if len(c.tokens) < 2 or any(len(t) < 2 for t in c.tokens):
            skipped.append((c.full_name, "incomplete name (first + last name required)"))
            continue
        eligible.append((ranked[0], ranked[1], ranked[2], c))

    # different people who could be confused (same first name + last initial) -> ambiguous
    ambiguous: set[str] = set()
    for i, (_, _, _, a) in enumerate(eligible):
        for _, _, _, b in eligible[i + 1 :]:
            if a.key != b.key and a.tokens[0] == b.tokens[0] and a.tokens[-1][0] == b.tokens[-1][0]:
                ambiguous |= {a.key, b.key}
    for _, _, _, c in eligible:
        if c.key in ambiguous:
            skipped.append((c.full_name, "ambiguous identity (similar senior person)"))
    clean = [e for e in eligible if e[3].key not in ambiguous]
    clean.sort(key=lambda e: (e[0], -e[2], -len(e[3].sources), e[3].key))

    selected: list[PersonIdentity] = []
    for rank, role, _, c in clean:
        if len(selected) >= config.max_persons_per_company:
            skipped.append((c.full_name, "max persons per company reached"))
            continue
        parts = display_name(c.full_name).split()
        selected.append(
            PersonIdentity(
                company_id=company_id,
                company_name=company_name,
                domain=domain,
                full_name=" ".join(parts),
                first_name=parts[0],
                last_name=" ".join(parts[1:]),
                title=c.title,
                role=role,
                role_rank=rank,
                sources=tuple(c.sources),
                source_ids=tuple(c.source_ids),
                email_hint=c.email_hint,
            )
        )
    return IdentityResolution(
        selected=selected,
        skipped=skipped,
        ambiguous=sorted({c.full_name for _, _, _, c in eligible if c.key in ambiguous}),
    )


# ------------------------------------------------------------------ result matching
def email_matches_domain(email: str | None, domain: str) -> bool:
    if not email or "@" not in email:
        return False
    host = email.rsplit("@", 1)[1].lower().strip(".")
    return host == domain.lower() or host.endswith("." + domain.lower())


def name_matches(
    identity: PersonIdentity, first: str | None, last: str | None, full: str | None
) -> bool:
    """False only on POSITIVE evidence of a different person (a provider echoing no name is
    not a mismatch)."""
    got = normalize_name(full or " ".join(x for x in (first, last) if x))
    if not got:
        return True
    want = normalize_name(identity.full_name).split()
    toks = got.split()
    if want[-1] not in toks and not (last and normalize_name(last) == want[-1]):
        return False
    return toks[0] == want[0] or toks[0][:1] == want[0][:1]
