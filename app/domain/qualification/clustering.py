"""Lightweight event clustering: one real-world event counts once, however many publishers
covered it (Phase 4 found the same ₹45 Cr round reported by four outlets).

Nothing is deleted - every source signal keeps its id and provenance and is listed inside its
cluster. Clusters are used only for qualification/scoring.

Two items (one item = one source URL, possibly carrying several signal types) are the same
event when their dates are within ``window_days`` and ANY of:
  * headline token similarity (Jaccard, company-name words removed) >= ``title_similarity``;
  * both report the same funding round amount;
  * they share a significant number (e.g. "1,000") AND at least two other key tokens.
Items without a date only merge on very high headline similarity (>= 0.8).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from app.domain.enums import SignalType
from app.domain.qualification.funding import FundingMention, extract_funding

# Priority used to name a cluster's primary type (first match wins).
TYPE_PRIORITY: tuple[SignalType, ...] = (
    SignalType.FUNDING,
    SignalType.AI_INTEREST,
    SignalType.EXPANSION,
    SignalType.PRODUCT_CUSTOMER,
    SignalType.TECHNOLOGY,
    SignalType.PAIN_POINT,
    SignalType.HIRING,
    SignalType.OTHER,
)
# Phase 4 evidence notes that prove attribution beyond a bare name match.
_ATTRIBUTION_NOTES = ("matches verified-website context", "mentions the verified domain")
_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "from", "at", "by",
    "its", "as", "is", "are", "be", "this", "that", "over", "into", "after", "new", "rs", "inr",
    "crore", "cr", "lakh", "million", "mn", "news", "india", "indian", "ltd", "pvt", "limited",
}  # fmt: skip
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_DATELESS_MIN_SIMILARITY = 0.8


@dataclass(frozen=True)
class SignalEvidence:
    """One persisted Phase 4 signal, as read for qualification (provenance kept)."""

    signal_id: UUID
    signal_type: SignalType
    summary: str
    evidence: str | None
    source_id: UUID
    url: str
    platform: str | None
    published_at: datetime | None
    confidence: float
    notes: tuple[str, ...] = ()
    channel: str | None = None

    @property
    def text(self) -> str:
        return f"{self.summary} {self.evidence or ''}".strip()

    @property
    def attributed(self) -> bool:
        return any(n.startswith(_ATTRIBUTION_NOTES) for n in self.notes)


@dataclass
class EventCluster:
    cluster_id: str
    signals: list[SignalEvidence]
    types: frozenset[SignalType]
    primary_type: SignalType
    event_date: datetime | None
    headline: str
    publishers: tuple[str, ...]
    confidence: float
    attributed: bool
    funding: list[FundingMention] = field(default_factory=list)

    @property
    def signal_ids(self) -> list[UUID]:
        return [s.signal_id for s in self.signals]

    @property
    def source_count(self) -> int:
        return len({s.source_id for s in self.signals})


def _tokens(text: str, name_words: set[str]) -> set[str]:
    words = re.findall(r"[a-z][a-z0-9]+", text.lower())
    return {w for w in words if w not in _STOP and w not in name_words and len(w) > 2}


def _numbers(text: str) -> set[str]:
    out = set()
    for n in _NUM_RE.findall(text):
        v = n.replace(",", "")
        if len(v.split(".")[0]) >= 2 and not (len(v) == 4 and v.startswith(("19", "20"))):
            out.add(v)  # significant, not a year
    return out


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


@dataclass
class _Item:
    signals: list[SignalEvidence]
    title: str
    date: datetime | None
    tokens: set[str]
    numbers: set[str]
    round_amount: tuple[str, Decimal] | None
    has_funding: bool


def _items(signals: Iterable[SignalEvidence], name_words: set[str]) -> list[_Item]:
    by_source: dict[UUID, list[SignalEvidence]] = {}
    for s in signals:
        by_source.setdefault(s.source_id, []).append(s)
    items = []
    for group in by_source.values():
        group.sort(key=lambda s: (-s.confidence, str(s.signal_id)))
        title = group[0].summary
        text = " ".join(s.text for s in group)
        mention = extract_funding(text)
        dates = [s.published_at for s in group if s.published_at is not None]
        items.append(
            _Item(
                signals=group,
                title=title,
                date=min(dates) if dates else None,
                tokens=_tokens(title, name_words),
                numbers=_numbers(title),
                round_amount=(
                    (mention.currency, mention.amount)
                    if mention and not mention.cumulative
                    else None
                ),
                has_funding=any(s.signal_type is SignalType.FUNDING for s in group),
            )
        )
    items.sort(key=lambda i: (i.date is None, i.date or datetime.max, i.title))
    return items


def _same_event(a: _Item, b: _Item, similarity: float, window: timedelta) -> bool:
    sim = _jaccard(a.tokens, b.tokens)
    if a.date is None or b.date is None:
        return sim >= _DATELESS_MIN_SIMILARITY
    if abs(a.date - b.date) > window:
        return False
    if sim >= similarity:
        return True
    if a.has_funding and b.has_funding and a.round_amount and a.round_amount == b.round_amount:
        return True
    return bool(a.numbers & b.numbers) and len(a.tokens & b.tokens) >= 2


def _cluster_id(signal_ids: Sequence[UUID]) -> str:
    joined = ",".join(sorted(str(i) for i in signal_ids))
    return "evt_" + hashlib.sha256(joined.encode()).hexdigest()[:12]


def cluster_signals(
    signals: Sequence[SignalEvidence],
    *,
    company_name: str,
    title_similarity: float = 0.5,
    window_days: int = 7,
) -> list[EventCluster]:
    """Group signals into events. Deterministic for a given input set."""
    name_words = set(re.findall(r"[a-z0-9]+", company_name.lower()))
    items = _items(signals, name_words)
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    window = timedelta(days=window_days)
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if _same_event(items[i], items[j], title_similarity, window):
                parent[find(j)] = find(i)

    groups: dict[int, list[_Item]] = {}
    for idx, item in enumerate(items):
        groups.setdefault(find(idx), []).append(item)

    clusters = []
    for members in groups.values():
        sigs = [s for m in members for s in m.signals]
        types = frozenset(s.signal_type for s in sigs)
        best = max(sigs, key=lambda s: (s.confidence, s.attributed))
        dates = [m.date for m in members if m.date is not None]
        mentions = []
        for m in members:
            found = extract_funding(" ".join(s.text for s in m.signals))
            if found is not None:
                mentions.append(found)
        clusters.append(
            EventCluster(
                cluster_id=_cluster_id([s.signal_id for s in sigs]),
                signals=sigs,
                types=types,
                primary_type=next(t for t in TYPE_PRIORITY if t in types),
                event_date=min(dates) if dates else None,
                headline=best.summary,
                publishers=tuple(sorted({s.platform or s.url for s in sigs})),
                confidence=max(s.confidence for s in sigs),  # corroboration != more confidence
                attributed=any(s.attributed for s in sigs),
                funding=mentions,
            )
        )
    clusters.sort(key=lambda c: (-(c.event_date.timestamp() if c.event_date else 0), c.cluster_id))
    return clusters
