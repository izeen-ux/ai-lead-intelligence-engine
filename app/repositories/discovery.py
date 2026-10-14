"""Discovery persistence: runs, deduplicated companies, deduplicated sources, provenance hits.

``DiscoveryStore`` is the protocol services depend on. Two implementations:

* ``SqlDiscoveryStore`` - PostgreSQL. Deduplication is enforced by unique constraints and
  atomic ``INSERT ... ON CONFLICT`` upserts, so concurrent runs cannot create duplicates.
* ``InMemoryDiscoveryStore`` - same semantics, for unit tests and dry runs.

Dedupe identities come from ``app.domain.identity``: company ``dedupe_key`` and source
``url_hash``. Every candidate produces a *hit* row (run x company x source) so provenance is
never lost, even when the company itself was already known.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import Boolean, Engine, func, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import NotFoundError
from app.db.models import (
    CompanyMergeRow,
    CompanyRow,
    DiscoveryHitRow,
    DiscoveryRunRow,
    SourceRow,
)
from app.domain.enums import DomainResolutionStatus, ProviderName, RunStatus
from app.domain.identity import canonicalize_url, company_dedupe_key, normalize_company_name
from app.domain.identity import url_hash as compute_url_hash
from app.domain.models import Company, DiscoveryRun, utcnow
from app.providers.contracts import CandidateRecord

# PostgreSQL trick: in INSERT ... ON CONFLICT ... RETURNING, ``xmax = 0`` is true only for rows
# that were freshly inserted (updated rows carry the updating transaction id).
_INSERTED = literal_column("(xmax = 0)", type_=Boolean).label("inserted")


@dataclass(frozen=True)
class PersistOutcome:
    company_id: UUID
    source_id: UUID
    new_company: bool
    new_source: bool
    new_hit: bool


class DiscoveryStore(Protocol):
    def create_run(self, run: DiscoveryRun) -> None: ...

    def save_run(self, run: DiscoveryRun) -> None: ...

    def get_run(self, run_id: UUID) -> DiscoveryRun: ...

    def persist_candidates(
        self,
        run_id: UUID,
        provider: ProviderName,
        provider_ref: str | None,
        candidates: Sequence[CandidateRecord],
    ) -> list[PersistOutcome]:
        """Persist a batch atomically (all or nothing)."""
        ...

    def get_company(self, company_id: UUID) -> Company: ...

    def find_company_by_key(self, dedupe_key: str) -> Company | None: ...

    def list_companies(self, limit: int = 100) -> list[Company]: ...

    def count_companies(self) -> int: ...


# ------------------------------------------------------------------------------ helpers
_RUN_FIELDS = (
    "provider",
    "actor_id",
    "provider_run_id",
    "status",
    "provider_status",
    "query",
    "provider_input",
    "max_items",
    "max_charge_usd",
    "started_at",
    "finished_at",
    "raw_item_count",
    "candidate_count",
    "new_company_count",
    "duplicate_count",
    "skipped_count",
    "estimated_cost_usd",
    "cost_usd",
    "charged_events",
    "error_code",
    "error_message",
)


def _run_values(run: DiscoveryRun) -> dict[str, Any]:
    data = run.model_dump(mode="python", include=set(_RUN_FIELDS))
    data["provider"] = run.provider.value
    data["status"] = run.status.value
    # JSONB columns need JSON-safe values (Decimals/datetimes inside dicts).
    dumped = run.model_dump(mode="json", include={"query", "provider_input", "charged_events"})
    data.update(dumped)
    return data


def _run_from_row(row: DiscoveryRunRow) -> DiscoveryRun:
    return DiscoveryRun(
        id=row.id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        provider=ProviderName(row.provider),
        actor_id=row.actor_id,
        provider_run_id=row.provider_run_id,
        status=RunStatus(row.status),
        provider_status=row.provider_status,
        query=row.query,
        provider_input=row.provider_input,
        max_items=row.max_items,
        max_charge_usd=row.max_charge_usd,
        started_at=row.started_at,
        finished_at=row.finished_at,
        raw_item_count=row.raw_item_count,
        candidate_count=row.candidate_count,
        new_company_count=row.new_company_count,
        duplicate_count=row.duplicate_count,
        skipped_count=row.skipped_count,
        estimated_cost_usd=row.estimated_cost_usd,
        cost_usd=row.cost_usd,
        charged_events=row.charged_events,
        error_code=row.error_code,
        error_message=row.error_message,
    )


def _company_from_row(row: CompanyRow, source_ids: list[UUID]) -> Company:
    return Company(
        id=row.id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        name=row.name,
        domain=row.domain,
        description=row.description,
        industry=row.industry,
        location=row.location,
        country_code=row.country_code,
        employee_count=row.employee_count,
        source_ids=source_ids,
        domain_status=DomainResolutionStatus(row.domain_status),
        domain_confidence=float(row.domain_confidence)
        if row.domain_confidence is not None
        else None,
    )


# ------------------------------------------------------------------------------ PostgreSQL
class SqlDiscoveryStore:
    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def create_run(self, run: DiscoveryRun) -> None:
        with self._sessions.begin() as s:
            s.add(DiscoveryRunRow(id=run.id, **_run_values(run)))

    def save_run(self, run: DiscoveryRun) -> None:
        with self._sessions.begin() as s:
            row = s.get(DiscoveryRunRow, run.id)
            if row is None:
                raise NotFoundError("discovery run not found", details={"id": str(run.id)})
            for key, value in _run_values(run).items():
                setattr(row, key, value)
            row.updated_at = utcnow()

    def get_run(self, run_id: UUID) -> DiscoveryRun:
        with self._sessions() as s:
            row = s.get(DiscoveryRunRow, run_id)
            if row is None:
                raise NotFoundError("discovery run not found", details={"id": str(run_id)})
            return _run_from_row(row)

    def persist_candidates(
        self,
        run_id: UUID,
        provider: ProviderName,
        provider_ref: str | None,
        candidates: Sequence[CandidateRecord],
    ) -> list[PersistOutcome]:
        outcomes: list[PersistOutcome] = []
        with self._sessions.begin() as s:
            for cand in candidates:
                source_id, new_source = self._upsert_source(s, cand, provider, provider_ref)
                company_id, new_company = self._upsert_company(s, cand)
                hit = (
                    insert(DiscoveryHitRow)
                    .values(
                        id=uuid.uuid4(),
                        run_id=run_id,
                        company_id=company_id,
                        source_id=source_id,
                        was_new_company=new_company,
                        candidate=cand.model_dump(mode="json"),
                    )
                    .on_conflict_do_nothing(constraint="uq_discovery_hits_run_company_source")
                    .returning(DiscoveryHitRow.id)
                )
                new_hit = s.execute(hit).scalar_one_or_none() is not None
                outcomes.append(
                    PersistOutcome(company_id, source_id, new_company, new_source, new_hit)
                )
        return outcomes

    @staticmethod
    def _upsert_source(
        s: Session, cand: CandidateRecord, provider: ProviderName, provider_ref: str | None
    ) -> tuple[UUID, bool]:
        url = str(cand.source_url)
        stmt = insert(SourceRow).values(
            id=uuid.uuid4(),
            url=canonicalize_url(url),
            url_hash=compute_url_hash(url),
            source_type=cand.source_type.value,
            platform=cand.source_platform,
            title=cand.source_title,
            published_at=cand.published_at,
            provider=provider.value,
            provider_ref=provider_ref,
        )
        upsert = stmt.on_conflict_do_update(
            index_elements=[SourceRow.url_hash],
            set_={
                "title": func.coalesce(SourceRow.title, stmt.excluded.title),
                "published_at": func.coalesce(SourceRow.published_at, stmt.excluded.published_at),
            },
        ).returning(SourceRow.id, _INSERTED)
        source_id, inserted = s.execute(upsert).one()
        return source_id, bool(inserted)

    @staticmethod
    def _existing_identity(s: Session, key: str, domain: str | None) -> UUID | None:
        """Company already known under this identity: a merge alias (Phase 3) for the key, or
        a verified domain. Keeps merged companies from being re-created by later discovery."""
        alias = s.execute(
            select(CompanyMergeRow.into_company_id).where(CompanyMergeRow.from_dedupe_key == key)
        ).scalar_one_or_none()
        if alias is not None:
            return alias
        if domain:
            return s.execute(
                select(CompanyRow.id).where(CompanyRow.domain == domain)
            ).scalar_one_or_none()
        return None

    @staticmethod
    def _upsert_company(s: Session, cand: CandidateRecord) -> tuple[UUID, bool]:
        now = utcnow()
        key = company_dedupe_key(cand.company_name, cand.domain)
        existing = SqlDiscoveryStore._existing_identity(s, key, cand.domain)
        if existing is not None:
            s.execute(
                update(CompanyRow)
                .where(CompanyRow.id == existing)
                .values(times_seen=CompanyRow.times_seen + 1, last_seen_at=now, updated_at=now)
            )
            return existing, False
        stmt = insert(CompanyRow).values(
            id=uuid.uuid4(),
            dedupe_key=key,
            name=cand.company_name,
            normalized_name=normalize_company_name(cand.company_name),
            domain=cand.domain,
            times_seen=1,
            first_seen_at=now,
            last_seen_at=now,
        )
        upsert = stmt.on_conflict_do_update(
            index_elements=[CompanyRow.dedupe_key],
            set_={
                "times_seen": CompanyRow.times_seen + 1,
                "last_seen_at": now,
                "updated_at": now,
                "domain": func.coalesce(CompanyRow.domain, stmt.excluded.domain),
            },
        ).returning(CompanyRow.id, _INSERTED)
        company_id, inserted = s.execute(upsert).one()
        return company_id, bool(inserted)

    def _source_ids(self, s: Session, company_ids: list[UUID]) -> dict[UUID, list[UUID]]:
        rows = s.execute(
            select(DiscoveryHitRow.company_id, DiscoveryHitRow.source_id)
            .where(DiscoveryHitRow.company_id.in_(company_ids))
            .distinct()
        ).all()
        result: dict[UUID, list[UUID]] = {cid: [] for cid in company_ids}
        for cid, sid in rows:
            result[cid].append(sid)
        return result

    def get_company(self, company_id: UUID) -> Company:
        with self._sessions() as s:
            row = s.get(CompanyRow, company_id)
            if row is None:
                raise NotFoundError("company not found", details={"id": str(company_id)})
            return _company_from_row(row, self._source_ids(s, [row.id])[row.id])

    def find_company_by_key(self, dedupe_key: str) -> Company | None:
        with self._sessions() as s:
            row = s.execute(
                select(CompanyRow).where(CompanyRow.dedupe_key == dedupe_key)
            ).scalar_one_or_none()
            if row is None:
                return None
            return _company_from_row(row, self._source_ids(s, [row.id])[row.id])

    def list_companies(self, limit: int = 100) -> list[Company]:
        with self._sessions() as s:
            rows = list(
                s.execute(
                    select(CompanyRow).order_by(CompanyRow.last_seen_at.desc()).limit(limit)
                ).scalars()
            )
            sources = self._source_ids(s, [r.id for r in rows])
            return [_company_from_row(r, sources[r.id]) for r in rows]

    def count_companies(self) -> int:
        with self._sessions() as s:
            return int(s.execute(select(func.count()).select_from(CompanyRow)).scalar_one())


# ------------------------------------------------------------------------------ in-memory
@dataclass
class _MemCompany:
    company: Company
    dedupe_key: str
    times_seen: int = 1
    last_seen_at: datetime = field(default_factory=utcnow)


class InMemoryDiscoveryStore:
    """Behaviourally equivalent to ``SqlDiscoveryStore`` (minus concurrency guarantees)."""

    def __init__(self) -> None:
        self.runs: dict[UUID, DiscoveryRun] = {}
        self._companies: dict[str, _MemCompany] = {}
        self._sources: dict[str, UUID] = {}
        self._hits: set[tuple[UUID, UUID, UUID]] = set()
        self.aliases: dict[str, str] = {}  # merged dedupe_key -> surviving dedupe_key

    def create_run(self, run: DiscoveryRun) -> None:
        self.runs[run.id] = run.model_copy(deep=True)

    def save_run(self, run: DiscoveryRun) -> None:
        if run.id not in self.runs:
            raise NotFoundError("discovery run not found", details={"id": str(run.id)})
        self.runs[run.id] = run.model_copy(deep=True)

    def get_run(self, run_id: UUID) -> DiscoveryRun:
        try:
            return self.runs[run_id].model_copy(deep=True)
        except KeyError:
            raise NotFoundError("discovery run not found", details={"id": str(run_id)}) from None

    def persist_candidates(
        self,
        run_id: UUID,
        provider: ProviderName,
        provider_ref: str | None,
        candidates: Sequence[CandidateRecord],
    ) -> list[PersistOutcome]:
        outcomes: list[PersistOutcome] = []
        for cand in candidates:
            h = compute_url_hash(str(cand.source_url))
            new_source = h not in self._sources
            source_id = self._sources.setdefault(h, uuid.uuid4())
            key = self.aliases.get(company_dedupe_key(cand.company_name, cand.domain))
            key = key or company_dedupe_key(cand.company_name, cand.domain)
            existing = self._companies.get(key)
            if existing is None:
                existing = _MemCompany(
                    Company(name=cand.company_name, domain=cand.domain), dedupe_key=key
                )
                self._companies[key] = existing
                new_company = True
            else:
                existing.times_seen += 1
                existing.last_seen_at = utcnow()
                new_company = False
            if source_id not in existing.company.source_ids:
                existing.company.source_ids.append(source_id)
            hit = (run_id, existing.company.id, source_id)
            new_hit = hit not in self._hits
            self._hits.add(hit)
            outcomes.append(
                PersistOutcome(existing.company.id, source_id, new_company, new_source, new_hit)
            )
        return outcomes

    def get_company(self, company_id: UUID) -> Company:
        for mem in self._companies.values():
            if mem.company.id == company_id:
                return mem.company.model_copy(deep=True)
        raise NotFoundError("company not found", details={"id": str(company_id)})

    def find_company_by_key(self, dedupe_key: str) -> Company | None:
        mem = self._companies.get(dedupe_key)
        return mem.company.model_copy(deep=True) if mem else None

    def list_companies(self, limit: int = 100) -> list[Company]:
        ordered = sorted(self._companies.values(), key=lambda m: m.last_seen_at, reverse=True)
        return [m.company.model_copy(deep=True) for m in ordered[:limit]]

    def count_companies(self) -> int:
        return len(self._companies)

    def times_seen(self, dedupe_key: str) -> int:
        return self._companies[dedupe_key].times_seen
