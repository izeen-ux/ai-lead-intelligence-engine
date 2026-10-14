"""Lead-analysis persistence (Phase 7). Analyses are INSERT-ONLY (trigger from migration 0007).

Company/signal/page/contact reads reuse the Phase 5/6 stores; this store adds the eligible
candidate query (latest decision QUALIFIED + a VERIFIED contact under that same decision),
runs, immutable analysis records and month-to-date cost.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import sessionmaker

from app.core.errors import NotFoundError
from app.db.models import AnalysisRunRow, CompanyRow, LeadAnalysisRow
from app.domain.analysis.records import AnalysisRunRecord, AnalysisStatus, LeadAnalysisRecord


class AnalysisStore(Protocol):
    def eligible_company_ids(self, limit: int) -> list[UUID]: ...

    def create_run(self, run: AnalysisRunRecord) -> None: ...

    def finish_run(self, run: AnalysisRunRecord) -> None: ...

    def insert_analysis(self, record: LeadAnalysisRecord) -> None: ...

    def analyses(self, company_id: UUID) -> list[LeadAnalysisRecord]: ...

    def month_cost(self, since: datetime) -> Decimal: ...


def _run_values(run: AnalysisRunRecord) -> dict[str, Any]:
    data = run.model_dump(mode="json", exclude={"created_at", "updated_at"})
    data.update(
        id=run.id, started_at=run.started_at, finished_at=run.finished_at, cost_usd=run.cost_usd
    )
    return data


def _dec(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(round(value, 3)))


class SqlAnalysisStore:
    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def eligible_company_ids(self, limit: int) -> list[UUID]:
        sql = text(
            "SELECT latest.company_id FROM ("
            "  SELECT DISTINCT ON (company_id) id, company_id, decision, decided_at"
            "  FROM qualification_decisions ORDER BY company_id, decided_at DESC, id DESC"
            ") latest WHERE latest.decision = 'qualified' AND EXISTS ("
            "  SELECT 1 FROM enrichment_contacts c WHERE c.company_id = latest.company_id"
            "  AND c.qualification_id = latest.id AND c.status = 'verified'"
            ") ORDER BY latest.decided_at DESC LIMIT :limit"
        )
        with self._sessions() as s:
            return [UUID(str(r[0])) for r in s.execute(sql, {"limit": limit})]

    def create_run(self, run: AnalysisRunRecord) -> None:
        with self._sessions.begin() as s:
            s.add(AnalysisRunRow(**_run_values(run)))

    def finish_run(self, run: AnalysisRunRecord) -> None:
        with self._sessions.begin() as s:
            row = s.get(AnalysisRunRow, run.id)
            if row is None:
                raise NotFoundError("analysis run not found", details={"id": str(run.id)})
            for key, value in _run_values(run).items():
                setattr(row, key, value)

    def insert_analysis(self, record: LeadAnalysisRecord) -> None:
        data = record.model_dump(mode="json", exclude={"created_at", "updated_at", "company_name"})
        data.update(
            id=record.id,
            run_id=record.run_id,
            company_id=record.company_id,
            qualification_id=record.qualification_id,
            contact_id=record.contact_id,
            confidence=_dec(record.confidence),
            cost_usd=record.cost_usd,
            analyzed_at=record.analyzed_at,
        )
        with self._sessions.begin() as s:
            s.add(LeadAnalysisRow(**data))

    def analyses(self, company_id: UUID) -> list[LeadAnalysisRecord]:
        with self._sessions() as s:
            rows = s.execute(
                select(LeadAnalysisRow, CompanyRow.name)
                .join(CompanyRow, CompanyRow.id == LeadAnalysisRow.company_id)
                .where(LeadAnalysisRow.company_id == company_id)
                .order_by(LeadAnalysisRow.analyzed_at.desc(), LeadAnalysisRow.id)
            ).tuples()
            return [
                LeadAnalysisRecord(
                    id=r.id,
                    created_at=r.created_at,
                    updated_at=r.created_at,
                    run_id=r.run_id,
                    company_id=r.company_id,
                    company_name=name,
                    qualification_id=r.qualification_id,
                    contact_id=r.contact_id,
                    status=AnalysisStatus(r.status),
                    analysis=r.analysis,
                    recommended_offer_id=r.recommended_offer_id,
                    primary_trigger=r.primary_trigger,
                    confidence=None if r.confidence is None else float(r.confidence),
                    supporting_evidence_ids=[str(x) for x in r.supporting_evidence_ids],
                    evidence_bundle=dict(r.evidence_bundle),
                    evidence_signal_ids=[str(x) for x in r.evidence_signal_ids],
                    catalog_version=r.catalog_version,
                    model=r.model,
                    gateway=r.gateway,
                    provider=r.provider,
                    usage={str(k): int(v) for k, v in r.usage.items()},
                    latency_ms=r.latency_ms,
                    cost_usd=r.cost_usd,
                    attempts=r.attempts,
                    policy_version=r.policy_version,
                    prompt_version=r.prompt_version,
                    error_code=r.error_code,
                    error_message=r.error_message,
                    raw_excerpt=r.raw_excerpt,
                    analyzed_at=r.analyzed_at,
                )
                for r, name in rows
            ]

    def month_cost(self, since: datetime) -> Decimal:
        with self._sessions() as s:
            value = s.execute(
                select(func.coalesce(func.sum(LeadAnalysisRow.cost_usd), 0)).where(
                    LeadAnalysisRow.analyzed_at >= since
                )
            ).scalar_one()
        return Decimal(value or 0)


@dataclass
class InMemoryAnalysisStore:
    eligible: list[UUID] = field(default_factory=list)
    runs: dict[UUID, AnalysisRunRecord] = field(default_factory=dict)
    records: list[LeadAnalysisRecord] = field(default_factory=list)
    prior_month_cost: Decimal = Decimal(0)

    def eligible_company_ids(self, limit: int) -> list[UUID]:
        return self.eligible[:limit]

    def create_run(self, run: AnalysisRunRecord) -> None:
        self.runs[run.id] = run.model_copy(deep=True)

    def finish_run(self, run: AnalysisRunRecord) -> None:
        self.runs[run.id] = run.model_copy(deep=True)

    def insert_analysis(self, record: LeadAnalysisRecord) -> None:
        if any(r.id == record.id for r in self.records):
            raise ValueError("lead analyses are append-only")
        self.records.append(record.model_copy(deep=True))

    def analyses(self, company_id: UUID) -> list[LeadAnalysisRecord]:
        return [r for r in reversed(self.records) if r.company_id == company_id]

    def month_cost(self, since: datetime) -> Decimal:
        return self.prior_month_cost + sum(
            (r.cost_usd or Decimal(0) for r in self.records if r.analyzed_at >= since), Decimal(0)
        )
