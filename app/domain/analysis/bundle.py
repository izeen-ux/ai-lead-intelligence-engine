"""Analysis evidence bundle - compact, deduplicated, every item carrying a stable evidence id.

Contents (no raw database dumps, no secrets, no contact email/phone):
  * company: name, verified domain
  * qualification: decision id, path, top reasons            -> id ``qual_<12 hex>``
  * events: Phase 4 signals CLUSTERED (duplicate coverage = one event) with dates, types,
    headline, publisher count, up to 3 source URLs           -> id ``evt_<12 hex>``
  * pages: short excerpts of the company's own crawled pages -> id ``page_<12 hex>``
  * contact: verified decision-maker name / title / role     -> id ``contact_<12 hex>``
The model may cite ONLY these ids; anything else is rejected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from app.domain.analysis.config import AnalysisConfig
from app.domain.enrichment.identity import PageText
from app.domain.enrichment.records import EnrichmentContactRecord
from app.domain.qualification.clustering import EventCluster, SignalEvidence, cluster_signals
from app.domain.qualification.records import QualificationDecisionRecord

_PAGE_PRIORITY = ("about", "team", "platform", "product", "solution", "service", "")


def short_id(prefix: str, value: UUID) -> str:
    return f"{prefix}_{value.hex[:12]}"


def _excerpt(markdown: str, limit: int) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", markdown)  # images
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links -> text
    text = re.sub(r"[#*_>`|]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].rsplit(" ", 1)[0] if len(text) > limit else text


def _page_rank(url: str) -> int:
    path = url.lower().split("://", 1)[-1].split("/", 1)[-1]
    if path in ("", "/"):
        return -1  # home first
    return next(
        (i for i, key in enumerate(_PAGE_PRIORITY) if key and key in path), len(_PAGE_PRIORITY)
    )


@dataclass(frozen=True)
class AnalysisInput:
    company_id: UUID
    company_name: str
    domain: str
    decision: QualificationDecisionRecord
    contact: EnrichmentContactRecord
    signals: list[SignalEvidence] = field(default_factory=list)
    pages: list[PageText] = field(default_factory=list)


@dataclass(frozen=True)
class AnalysisBundle:
    payload: dict[str, Any]  # exactly what the model sees
    allowed_ids: frozenset[str]
    event_ids: tuple[str, ...]
    signal_ids: tuple[str, ...]  # provenance: underlying Phase 4 signal UUIDs
    duplicate_signals_merged: int

    @property
    def evidence_count(self) -> int:
        return len(self.allowed_ids)


def _event(c: EventCluster) -> dict[str, Any]:
    return {
        "id": c.cluster_id,
        "date": c.event_date.date().isoformat() if c.event_date else None,
        "types": sorted(t.value for t in c.types),
        "headline": c.headline[:200],
        "publishers": len(c.publishers),
        "sources": sorted({s.url for s in c.signals})[:3],
        "about_this_company": c.attributed,
    }


def build_bundle(data: AnalysisInput, config: AnalysisConfig) -> AnalysisBundle:
    clusters = cluster_signals(data.signals, company_name=data.company_name)
    events = clusters[: config.max_events]
    pages = sorted(data.pages, key=lambda p: (_page_rank(p.url), p.url))[:3]
    page_items = []
    for p in pages:
        pid = short_id("page", p.page_id or uuid5(NAMESPACE_URL, p.url))  # stable across runs
        excerpt = _excerpt(p.markdown, config.max_page_chars)
        if excerpt:
            page_items.append({"id": pid, "url": p.url, "excerpt": excerpt})
    qual_id = short_id("qual", data.decision.id)
    contact_id = short_id("contact", data.contact.id)
    payload: dict[str, Any] = {
        "company": {"name": data.company_name, "domain": data.domain},
        "qualification": {
            "id": qual_id,
            "decision": data.decision.decision.value,
            "path": data.decision.path.value,
            "reasons": data.decision.reasons[:5],
        },
        "events": [_event(c) for c in events],
        "pages": page_items,
        "contact": {
            "id": contact_id,
            "name": data.contact.full_name,
            "title": data.contact.title,
            "role": data.contact.role,
        },
    }
    allowed = {
        qual_id,
        contact_id,
        *(e["id"] for e in payload["events"]),
        *(p["id"] for p in page_items),
    }
    return AnalysisBundle(
        payload=payload,
        allowed_ids=frozenset(allowed),
        event_ids=tuple(c.cluster_id for c in events),
        signal_ids=tuple(str(s) for c in events for s in c.signal_ids),
        duplicate_signals_merged=sum(max(c.source_count - 1, 0) for c in events),
    )
