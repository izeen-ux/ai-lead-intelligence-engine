"""Qualification persistence (Phase 5).

* Reads companies + Phase 4 signals (joined to their source rows for provenance).
* Writes qualification runs and INSERT-ONLY decisions (history is never overwritten; the
  PostgreSQL trigger from migration 0005 rejects UPDATE on ``qualification_decisions``).
* ``latest_decision`` is what Phase 6 must consult before any enrichment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from sqlalchemy import Engine, case, desc, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import NotFoundError
from app.db.models import (
    CompanyRow,
    QualificationDecisionRow,
    QualificationRunRow,
    SignalRow,
    SourceRow,
    WebsitePageRow,
)
from app.domain.enums import (
    DomainResolutionStatus,
    QualificationDecision,
    QualificationPath,
    SignalType,
    StageStatus,
)
from app.domain.qualification.clustering import SignalEvidence
from app.domain.qualification.records import QualificationDecisionRecord, QualificationRunRecord
from app.domain.qualification.rules import CompanyFacts


@dataclass(frozen=True)
class QualificationInput:
    facts: CompanyFacts
    signals: list[SignalEvidence]
    # What the verified website says about the company (for the AI attribution check only).
    profile: dict[str, str] = field(default_factory=dict)


class QualificationStore(Protocol):
    def candidate_ids(self, limit: int) -> list[UUID]: ...

    def load(self, company_id: UUID) -> QualificationInput: ...

    def create_run(self, run: QualificationRunRecord) -> None: ...

    def finish_run(self, run: QualificationRunRecord) -> None: ...

    def insert_decision(self, decision: QualificationDecisionRecord) -> None: ...

    def latest_decision(self, company_id: UUID) -> QualificationDecisionRecord | None: ...

    def history(self, company_id: UUID) -> list[QualificationDecisionRecord]: ...


def _dec(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(round(value, 3)))


def _flt(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _run_values(run: QualificationRunRecord) -> dict[str, object]:
    data = run.model_dump(mode="json", exclude={"created_at", "updated_at"})
    data.update(id=run.id, started_at=run.started_at, finished_at=run.finished_at)
    return data


def _decision_row(d: QualificationDecisionRecord) -> QualificationDecisionRow:
    return QualificationDecisionRow(
        id=d.id,
        run_id=d.run_id,
        company_id=d.company_id,
        decision=d.decision.value,
        path=d.path.value,
        hard_rules_passed=d.hard_rules_passed,
        hard_rule_results=d.hard_rule_results,
        reasons=d.reasons,
        laya_status=d.laya_status.value,
        laya_result=d.laya_result,
        laya_confidence=_dec(d.laya_confidence),
        laya_model_version=d.laya_model_version,
        llm_review_status=d.llm_review_status.value,
        llm_review=d.llm_review,
        score=_dec(d.score),
        confidence=_dec(d.confidence),
        policy_version=d.policy_version,
        icp_config=d.icp_config,
        evidence_bundle=d.evidence_bundle,
        evidence_signal_ids=[str(i) for i in d.evidence_signal_ids],
        signal_count=d.signal_count,
        event_count=d.event_count,
        decided_at=d.decided_at,
    )


def _decision_record(r: QualificationDecisionRow, name: str) -> QualificationDecisionRecord:
    return QualificationDecisionRecord(
        id=r.id,
        created_at=r.created_at,
        updated_at=r.created_at,
        run_id=r.run_id,
        company_id=r.company_id,
        company_name=name,
        decision=QualificationDecision(r.decision),
        path=QualificationPath(r.path),
        hard_rules_passed=r.hard_rules_passed,
        hard_rule_results=list(r.hard_rule_results),
        reasons=[str(x) for x in r.reasons],
        laya_status=StageStatus(r.laya_status),
        laya_result=r.laya_result,
        laya_confidence=_flt(r.laya_confidence),
        laya_model_version=r.laya_model_version,
        llm_review_status=StageStatus(r.llm_review_status),
        llm_review=r.llm_review,
        score=_flt(r.score),
        confidence=_flt(r.confidence),
        policy_version=r.policy_version,
        icp_config=dict(r.icp_config),
        evidence_bundle=dict(r.evidence_bundle),
        evidence_signal_ids=[UUID(str(x)) for x in r.evidence_signal_ids],
        signal_count=r.signal_count,
        event_count=r.event_count,
        decided_at=r.decided_at,
    )


# ------------------------------------------------------------------------------ PostgreSQL
class SqlQualificationStore:
    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def candidate_ids(self, limit: int) -> list[UUID]:
        verified_first = case(
            (CompanyRow.domain_status == DomainResolutionStatus.VERIFIED.value, 0), else_=1
        )
        with self._sessions() as s:
            return list(
                s.execute(
                    select(CompanyRow.id)
                    .order_by(
                        verified_first,
                        CompanyRow.signals_researched_at.desc().nulls_last(),
                        CompanyRow.name,
                    )
                    .limit(limit)
                ).scalars()
            )

    def load(self, company_id: UUID) -> QualificationInput:
        with self._sessions() as s:
            row = s.get(CompanyRow, company_id)
            if row is None:
                raise NotFoundError("company not found", details={"id": str(company_id)})
            signals = [
                SignalEvidence(
                    signal_id=sig.id,
                    signal_type=SignalType(sig.signal_type),
                    summary=sig.summary,
                    evidence=sig.evidence,
                    source_id=src.id,
                    url=src.url,
                    platform=sig.platform or src.platform,
                    published_at=sig.published_at or src.published_at,
                    confidence=float(sig.confidence),
                    notes=tuple(str(n) for n in sig.evidence_notes),
                    channel=sig.channel,
                )
                for sig, src in s.execute(
                    select(SignalRow, SourceRow)
                    .join(SourceRow, SourceRow.id == SignalRow.source_id)
                    .where(SignalRow.company_id == company_id)
                    .order_by(SignalRow.created_at, SignalRow.id)
                ).tuples()
            ]
            facts = CompanyFacts(
                company_id=row.id,
                name=row.name,
                domain=row.domain,
                domain_status=DomainResolutionStatus(row.domain_status),
                country_code=row.country_code,
            )
            page = s.execute(
                select(WebsitePageRow)
                .where(WebsitePageRow.company_id == company_id)
                .order_by((WebsitePageRow.page_type != "home"), WebsitePageRow.fetched_at)
                .limit(1)
            ).scalar_one_or_none()
            profile = {
                "description": row.description or "",
                "industry": row.industry or "",
                "location": row.location or "",
                "website_title": (page.title or "") if page else "",
                "website_description": (page.description or "") if page else "",
                "website_text": " ".join((page.markdown or "").split())[:900] if page else "",
            }
            return QualificationInput(facts, signals, {k: v[:900] for k, v in profile.items() if v})

    def create_run(self, run: QualificationRunRecord) -> None:
        with self._sessions.begin() as s:
            s.add(QualificationRunRow(**_run_values(run)))

    def finish_run(self, run: QualificationRunRecord) -> None:
        with self._sessions.begin() as s:
            row = s.get(QualificationRunRow, run.id)
            if row is None:
                raise NotFoundError("qualification run not found", details={"id": str(run.id)})
            for key, value in _run_values(run).items():
                setattr(row, key, value)

    def insert_decision(self, decision: QualificationDecisionRecord) -> None:
        with self._sessions.begin() as s:
            s.add(_decision_row(decision))

    def _records(
        self, s: Session, company_id: UUID, limit: int | None
    ) -> list[QualificationDecisionRecord]:
        stmt = (
            select(QualificationDecisionRow, CompanyRow.name)
            .join(CompanyRow, CompanyRow.id == QualificationDecisionRow.company_id)
            .where(QualificationDecisionRow.company_id == company_id)
            .order_by(desc(QualificationDecisionRow.decided_at), desc(QualificationDecisionRow.id))
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return [_decision_record(r, name) for r, name in s.execute(stmt).tuples()]

    def latest_decision(self, company_id: UUID) -> QualificationDecisionRecord | None:
        with self._sessions() as s:
            found = self._records(s, company_id, 1)
            return found[0] if found else None

    def history(self, company_id: UUID) -> list[QualificationDecisionRecord]:
        with self._sessions() as s:
            return self._records(s, company_id, None)


# ------------------------------------------------------------------------------ in-memory
@dataclass
class InMemoryQualificationStore:
    inputs: dict[UUID, QualificationInput] = field(default_factory=dict)
    runs: dict[UUID, QualificationRunRecord] = field(default_factory=dict)
    decisions: list[QualificationDecisionRecord] = field(default_factory=list)

    def add(self, item: QualificationInput) -> None:
        self.inputs[item.facts.company_id] = item

    def candidate_ids(self, limit: int) -> list[UUID]:
        ordered = sorted(self.inputs.values(), key=lambda i: (not i.facts.verified, i.facts.name))
        return [i.facts.company_id for i in ordered][:limit]

    def load(self, company_id: UUID) -> QualificationInput:
        try:
            return self.inputs[company_id]
        except KeyError:
            raise NotFoundError("company not found", details={"id": str(company_id)}) from None

    def create_run(self, run: QualificationRunRecord) -> None:
        self.runs[run.id] = run.model_copy(deep=True)

    def finish_run(self, run: QualificationRunRecord) -> None:
        self.runs[run.id] = run.model_copy(deep=True)

    def insert_decision(self, decision: QualificationDecisionRecord) -> None:
        if any(d.id == decision.id for d in self.decisions):
            raise ValueError("qualification decisions are append-only")
        self.decisions.append(decision.model_copy(deep=True))

    def history(self, company_id: UUID) -> list[QualificationDecisionRecord]:
        found = [d for d in self.decisions if d.company_id == company_id]
        return sorted(found, key=lambda d: d.decided_at, reverse=True)

    def latest_decision(self, company_id: UUID) -> QualificationDecisionRecord | None:
        found = self.history(company_id)
        return found[0] if found else None
