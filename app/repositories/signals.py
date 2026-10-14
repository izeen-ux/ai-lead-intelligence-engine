"""Signal-research persistence (Phase 4).

* Research targets are read from ``companies`` - only VERIFIED companies with a canonical domain
  are ever returned as eligible (the service re-checks; defence in depth).
* Each evidence item becomes: a deduplicated ``sources`` row (url_hash) + one ``signals`` row
  per (company, signal_type, source) - duplicates across runs are skipped, not re-inserted.
* Every signal is built through the ``Signal`` domain model first, so its invariants (at least
  one source, confidence in [0, 1]) cannot be bypassed.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from sqlalchemy import Engine, desc, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import NotFoundError
from app.db.models import (
    CompanyRow,
    SignalResearchRunRow,
    SignalRow,
    SourceRow,
    WebsiteCrawlRow,
)
from app.domain.enums import (
    DomainResolutionStatus,
    ProviderName,
    ResearchChannel,
    SignalType,
    SourceType,
)
from app.domain.identity import canonicalize_url, url_hash
from app.domain.models import Signal, SignalResearchRunRecord, utcnow
from app.domain.signals import context_terms_from
from app.providers.contracts import EvidenceItem


@dataclass(frozen=True)
class ResearchTarget:
    company_id: UUID
    name: str
    normalized_name: str
    domain: str | None
    domain_status: DomainResolutionStatus
    context_terms: tuple[str, ...] = ()

    @property
    def is_verified(self) -> bool:
        return self.domain_status is DomainResolutionStatus.VERIFIED and bool(self.domain)


@dataclass(frozen=True)
class PersistCounts:
    created: int
    duplicate: int


class SignalStore(Protocol):
    def research_target(self, company_id: UUID) -> ResearchTarget: ...

    def verified_targets(self, limit: int) -> list[ResearchTarget]: ...

    def create_run(self, run: SignalResearchRunRecord) -> None: ...

    def save_run(self, run: SignalResearchRunRecord) -> None: ...

    def persist_signals(
        self, run_id: UUID, company_id: UUID, items: Sequence[EvidenceItem]
    ) -> PersistCounts: ...

    def list_signals(self, company_id: UUID) -> list[Signal]: ...


def _source_type(channel: str | None) -> SourceType:
    return SourceType.SOCIAL if channel == ResearchChannel.YOUTUBE.value else SourceType.NEWS


def _to_domain_signal(company_id: UUID, source_id: UUID, item: EvidenceItem) -> Signal:
    return Signal(
        company_id=company_id,
        signal_type=item.signal_type,
        summary=item.title or item.snippet,
        evidence=item.snippet,
        source_ids=[source_id],
        confidence=item.confidence,
        observed_at=item.published_at,
    )


def _run_values(run: SignalResearchRunRecord) -> dict[str, object]:
    data = run.model_dump(mode="json", exclude={"created_at", "updated_at"})
    data.update(id=run.id, company_id=run.company_id, started_at=run.started_at)
    data["finished_at"] = run.finished_at
    return data


# ------------------------------------------------------------------------------ PostgreSQL
class SqlSignalStore:
    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)

    @staticmethod
    def _target(s: Session, row: CompanyRow) -> ResearchTarget:
        facts = (
            s.execute(
                select(WebsiteCrawlRow.facts)
                .where(
                    WebsiteCrawlRow.company_id == row.id,
                    WebsiteCrawlRow.identity_verified.is_(True),
                )
                .order_by(desc(WebsiteCrawlRow.started_at))
                .limit(1)
            ).scalar_one_or_none()
            or {}
        )
        terms = context_terms_from(
            facts.get("home_title"), facts.get("home_description"), company_name=row.name
        )
        return ResearchTarget(
            company_id=row.id,
            name=row.name,
            normalized_name=row.normalized_name,
            domain=row.domain,
            domain_status=DomainResolutionStatus(row.domain_status),
            context_terms=tuple(terms),
        )

    def research_target(self, company_id: UUID) -> ResearchTarget:
        with self._sessions() as s:
            row = s.get(CompanyRow, company_id)
            if row is None:
                raise NotFoundError("company not found", details={"id": str(company_id)})
            return self._target(s, row)

    def verified_targets(self, limit: int) -> list[ResearchTarget]:
        with self._sessions() as s:
            rows = s.execute(
                select(CompanyRow)
                .where(
                    CompanyRow.domain_status == DomainResolutionStatus.VERIFIED.value,
                    CompanyRow.domain.is_not(None),
                )
                .order_by(CompanyRow.signals_researched_at.asc().nulls_first())
                .limit(limit)
            ).scalars()
            return [self._target(s, row) for row in rows]

    def create_run(self, run: SignalResearchRunRecord) -> None:
        with self._sessions.begin() as s:
            s.add(SignalResearchRunRow(**_run_values(run)))

    def save_run(self, run: SignalResearchRunRecord) -> None:
        with self._sessions.begin() as s:
            row = s.get(SignalResearchRunRow, run.id)
            if row is None:
                raise NotFoundError("research run not found", details={"id": str(run.id)})
            for key, value in _run_values(run).items():
                setattr(row, key, value)

    def persist_signals(
        self, run_id: UUID, company_id: UUID, items: Sequence[EvidenceItem]
    ) -> PersistCounts:
        created = duplicate = 0
        with self._sessions.begin() as s:
            for item in items:
                url = str(item.url)
                source_stmt = insert(SourceRow).values(
                    id=uuid.uuid4(),
                    url=canonicalize_url(url),
                    url_hash=url_hash(url),
                    source_type=_source_type(item.channel).value,
                    platform=item.platform,
                    title=item.title,
                    published_at=item.published_at,
                    provider=ProviderName.AGENT_REACH.value,
                    provider_ref=item.channel,
                )
                source_id = s.execute(
                    source_stmt.on_conflict_do_update(
                        index_elements=[SourceRow.url_hash],
                        set_={"title": func.coalesce(SourceRow.title, source_stmt.excluded.title)},
                    ).returning(SourceRow.id)
                ).scalar_one()
                signal = _to_domain_signal(company_id, source_id, item)  # invariants enforced
                inserted = s.execute(
                    insert(SignalRow)
                    .values(
                        id=signal.id,
                        company_id=company_id,
                        source_id=source_id,
                        run_id=run_id,
                        signal_type=signal.signal_type.value,
                        summary=signal.summary,
                        evidence=signal.evidence,
                        confidence=Decimal(str(signal.confidence)),
                        channel=item.channel,
                        platform=item.platform,
                        query=item.query,
                        published_at=item.published_at,
                        evidence_notes=item.evidence_notes,
                    )
                    .on_conflict_do_nothing(constraint="uq_signals_company_type_source")
                    .returning(SignalRow.id)
                ).scalar_one_or_none()
                if inserted is None:
                    duplicate += 1
                else:
                    created += 1
            s.execute(
                update(CompanyRow)
                .where(CompanyRow.id == company_id)
                .values(signals_researched_at=utcnow(), updated_at=utcnow())
            )
        return PersistCounts(created, duplicate)

    def list_signals(self, company_id: UUID) -> list[Signal]:
        with self._sessions() as s:
            rows = s.execute(
                select(SignalRow)
                .where(SignalRow.company_id == company_id)
                .order_by(SignalRow.created_at)
            ).scalars()
            return [
                Signal(
                    id=r.id,
                    company_id=r.company_id,
                    signal_type=SignalType(r.signal_type),
                    summary=r.summary,
                    evidence=r.evidence,
                    source_ids=[r.source_id],
                    confidence=float(r.confidence),
                    observed_at=r.published_at,
                )
                for r in rows
            ]


# ------------------------------------------------------------------------------ in-memory
@dataclass
class InMemorySignalStore:
    targets: dict[UUID, ResearchTarget] = field(default_factory=dict)
    runs: dict[UUID, SignalResearchRunRecord] = field(default_factory=dict)
    signals: dict[tuple[UUID, str, str], Signal] = field(default_factory=dict)
    sources: dict[str, UUID] = field(default_factory=dict)

    def add_target(self, target: ResearchTarget) -> None:
        self.targets[target.company_id] = target

    def research_target(self, company_id: UUID) -> ResearchTarget:
        try:
            return self.targets[company_id]
        except KeyError:
            raise NotFoundError("company not found", details={"id": str(company_id)}) from None

    def verified_targets(self, limit: int) -> list[ResearchTarget]:
        return [t for t in self.targets.values() if t.is_verified][:limit]

    def create_run(self, run: SignalResearchRunRecord) -> None:
        self.runs[run.id] = run.model_copy(deep=True)

    def save_run(self, run: SignalResearchRunRecord) -> None:
        self.runs[run.id] = run.model_copy(deep=True)

    def persist_signals(
        self, run_id: UUID, company_id: UUID, items: Sequence[EvidenceItem]
    ) -> PersistCounts:
        created = duplicate = 0
        for item in items:
            h = url_hash(str(item.url))
            source_id = self.sources.setdefault(h, uuid.uuid4())
            key = (company_id, item.signal_type.value, h)
            if key in self.signals:
                duplicate += 1
                continue
            self.signals[key] = _to_domain_signal(company_id, source_id, item)
            created += 1
        return PersistCounts(created, duplicate)

    def list_signals(self, company_id: UUID) -> list[Signal]:
        return [s for (cid, _, _), s in self.signals.items() if cid == company_id]
