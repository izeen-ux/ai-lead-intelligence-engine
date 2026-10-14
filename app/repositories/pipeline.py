"""Pipeline run persistence + the candidate queries the orchestrator uses (Phase 9).

Candidate queries pick only companies that NEED a stage, so a scheduled pipeline never
re-decides a human-reviewed company, re-enriches a lead that already has a verified contact or
re-analyses (and re-pays for) a lead that already has an accepted analysis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import Engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.core.errors import NotFoundError
from app.db.models import PipelineRunRow
from app.domain.pipeline import (
    PipelineAlreadyRunningError,
    PipelineRunRecord,
    PipelineStatus,
    Stage,
    StageResult,
    Trigger,
)
from app.domain.qualification.icp import POLICY_VERSION


class PipelineStore(Protocol):
    def create_run(self, run: PipelineRunRecord) -> None: ...

    def save_run(self, run: PipelineRunRecord) -> None: ...

    def get_run(self, run_id: UUID) -> PipelineRunRecord: ...

    def list_runs(self, limit: int) -> list[PipelineRunRecord]: ...

    def running(self) -> PipelineRunRecord | None: ...


def _values(run: PipelineRunRecord) -> dict[str, Any]:
    return {
        "id": run.id,
        "status": run.status.value,
        "active_slot": 1 if run.status is PipelineStatus.RUNNING else None,
        "trigger": run.trigger.value,
        "requested_by": run.requested_by,
        "mode": run.mode,
        "dry_run": run.dry_run,
        "stages_requested": [s.value for s in run.stages_requested],
        "company_limit": run.company_limit,
        "stages": [s.model_dump(mode="json") for s in run.stages],
        "config": run.config,
        "estimated_cost_usd": run.estimated_cost_usd,
        "error_code": run.error_code,
        "error_message": run.error_message,
        "started_at": run.started_at,
        "heartbeat_at": run.heartbeat_at,
        "finished_at": run.finished_at,
    }


def _record(r: PipelineRunRow) -> PipelineRunRecord:
    return PipelineRunRecord(
        id=r.id,
        status=PipelineStatus(r.status),
        trigger=Trigger(r.trigger),
        requested_by=r.requested_by,
        mode=r.mode,
        dry_run=r.dry_run,
        stages_requested=[Stage(s) for s in r.stages_requested],
        company_limit=r.company_limit,
        stages=[StageResult.model_validate(s) for s in r.stages],
        config=dict(r.config),
        estimated_cost_usd=Decimal(r.estimated_cost_usd),
        error_code=r.error_code,
        error_message=r.error_message,
        started_at=r.started_at,
        heartbeat_at=r.heartbeat_at,
        finished_at=r.finished_at,
    )


# Latest decision per company (same ordering as SqlQualificationStore.latest_decision).
_SIGNAL_CANDIDATES = text(
    "SELECT id FROM companies WHERE domain_status = 'verified' AND domain IS NOT NULL "
    "AND signals_researched_at IS NULL ORDER BY first_seen_at LIMIT :limit"
)
_QUALIFICATION_CANDIDATES = text(
    "WITH latest AS (SELECT DISTINCT ON (d.company_id) d.company_id, d.decided_at, r.laya_mode, "
    "d.laya_status, d.policy_version, d.decision, d.llm_review_status "
    "FROM qualification_decisions d "
    "JOIN qualification_runs r ON r.id = d.run_id "
    "ORDER BY d.company_id, d.decided_at DESC, d.id DESC) "
    "SELECT c.id FROM companies c LEFT JOIN latest l ON l.company_id = c.id "
    "WHERE c.domain_status = 'verified' AND c.signals_researched_at IS NOT NULL AND ("
    "l.company_id IS NULL OR (l.laya_mode <> 'human' AND ("
    "c.signals_researched_at > l.decided_at OR l.laya_status = 'error' "
    "OR l.policy_version <> :policy "
    "OR (:ai_review AND l.decision = 'review' AND (l.llm_review_status = 'skipped' OR ("
    "l.llm_review_status = 'error' AND l.decided_at < now() - interval '6 hours')))))"
    ") ORDER BY c.signals_researched_at DESC, c.name LIMIT :limit"
)
_ENRICHMENT_CANDIDATES = text(
    "WITH latest AS (SELECT DISTINCT ON (company_id) id, company_id, decision, decided_at "
    "FROM qualification_decisions ORDER BY company_id, decided_at DESC, id DESC) "
    "SELECT l.company_id FROM latest l WHERE l.decision = 'qualified' AND NOT EXISTS ("
    "SELECT 1 FROM enrichment_contacts c WHERE c.qualification_id = l.id "
    "AND c.status = 'verified') ORDER BY l.decided_at DESC LIMIT :limit"
)
_ANALYSIS_CANDIDATES = text(
    "WITH latest AS (SELECT DISTINCT ON (company_id) id, company_id, decision, decided_at "
    "FROM qualification_decisions ORDER BY company_id, decided_at DESC, id DESC) "
    "SELECT l.company_id FROM latest l WHERE l.decision = 'qualified' AND EXISTS ("
    "SELECT 1 FROM enrichment_contacts c WHERE c.qualification_id = l.id "
    "AND c.status = 'verified') AND NOT EXISTS (SELECT 1 FROM lead_analyses a "
    "WHERE a.qualification_id = l.id AND a.status = 'succeeded') "
    "ORDER BY l.decided_at DESC LIMIT :limit"
)


class SqlPipelineStore:
    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def create_run(self, run: PipelineRunRecord) -> None:
        try:
            with self._sessions.begin() as s:
                s.add(PipelineRunRow(**_values(run)))
        except IntegrityError as exc:
            if "uq_pipeline_runs_active_slot" in str(exc.orig):
                current = self.running()
                raise PipelineAlreadyRunningError(current.id if current else None) from None
            raise

    def save_run(self, run: PipelineRunRecord) -> None:
        with self._sessions.begin() as s:
            row = s.get(PipelineRunRow, run.id)
            if row is None:
                raise NotFoundError("pipeline run not found", details={"id": str(run.id)})
            for key, value in _values(run).items():
                setattr(row, key, value)

    def get_run(self, run_id: UUID) -> PipelineRunRecord:
        with self._sessions() as s:
            row = s.get(PipelineRunRow, run_id)
            if row is None:
                raise NotFoundError("pipeline run not found", details={"id": str(run_id)})
            return _record(row)

    def list_runs(self, limit: int) -> list[PipelineRunRecord]:
        with self._sessions() as s:
            rows = s.scalars(
                select(PipelineRunRow).order_by(PipelineRunRow.started_at.desc()).limit(limit)
            )
            return [_record(r) for r in rows]

    def running(self) -> PipelineRunRecord | None:
        with self._sessions() as s:
            row = s.scalars(
                select(PipelineRunRow).where(PipelineRunRow.status == "running").limit(1)
            ).first()
            return _record(row) if row is not None else None

    # ------------------------------------------------------------------ candidates
    def _ids(self, sql: Any, limit: int, **params: Any) -> list[UUID]:
        with self._sessions() as s:
            return [UUID(str(r[0])) for r in s.execute(sql, {"limit": limit, **params})]

    def discovery_run_count(self, source: str) -> int:
        """Discovery runs already recorded for a source (rotates the ICP query set)."""
        sql = text("SELECT count(*) FROM discovery_runs WHERE query->>'source' = :source")
        with self._sessions() as s:
            return int(s.execute(sql, {"source": source}).scalar_one())

    # ------------------------------------------------------------------ lead owners
    def claim_new_companies(self, discovery_run_id: UUID, owner: str, run_id: UUID) -> int:
        """Companies FIRST discovered by this discovery run now belong to ``owner``."""
        sql = text(
            "INSERT INTO lead_owners (company_id, owner, pipeline_run_id) "
            "SELECT DISTINCT h.company_id, :owner, CAST(:run AS uuid) FROM discovery_hits h "
            "WHERE h.run_id = :discovery AND h.was_new_company "
            "ON CONFLICT (company_id) DO NOTHING"
        )
        with self._sessions.begin() as s:
            params = {"owner": owner, "run": str(run_id), "discovery": discovery_run_id}
            return int(s.execute(sql, params).rowcount or 0)  # type: ignore[attr-defined]

    def claim_unowned(self, company_ids: list[UUID], owner: str, run_id: UUID) -> int:
        """Give ``owner`` the companies of this run nobody owns yet (first owner is kept)."""
        if not company_ids:
            return 0
        sql = text(
            "INSERT INTO lead_owners (company_id, owner, pipeline_run_id) "
            "SELECT c.id, :owner, CAST(:run AS uuid) FROM companies c WHERE c.id = ANY(:ids) "
            "ON CONFLICT (company_id) DO NOTHING"
        )
        with self._sessions.begin() as s:
            params = {"owner": owner, "run": str(run_id), "ids": company_ids}
            return int(s.execute(sql, params).rowcount or 0)  # type: ignore[attr-defined]

    def signal_candidates(self, limit: int) -> list[UUID]:
        """VERIFIED companies whose signals were never researched."""
        return self._ids(_SIGNAL_CANDIDATES, limit)

    # Set by the pipeline when LLM_REVIEW_ENABLED: REVIEW decisions the AI reviewer never saw
    # (made before it was switched on) are decided again; a failed AI call is retried after
    # 6 hours. A REVIEW the AI looked at and left open stays with a human.
    ai_review: bool = False

    def qualification_candidates(self, limit: int) -> list[UUID]:
        """VERIFIED + researched companies never decided, researched again after their last
        AUTOMATED decision, or whose last automated decision was made WITHOUT the classifier
        because it errored / timed out (a provisional fail-closed REVIEW that deserves a real
        evaluation), or was made under an OLDER qualification policy version. A human review
        decision is never re-decided by the pipeline."""
        return self._ids(
            _QUALIFICATION_CANDIDATES, limit, policy=POLICY_VERSION, ai_review=self.ai_review
        )

    def enrichment_candidates(self, limit: int) -> list[UUID]:
        """Latest decision QUALIFIED and no VERIFIED contact under that decision yet."""
        return self._ids(_ENRICHMENT_CANDIDATES, limit)

    def analysis_candidates(self, limit: int) -> list[UUID]:
        """QUALIFIED + VERIFIED contact, no accepted analysis for that decision yet."""
        return self._ids(_ANALYSIS_CANDIDATES, limit)


@dataclass
class InMemoryPipelineStore:
    runs: dict[UUID, PipelineRunRecord] = field(default_factory=dict)

    def create_run(self, run: PipelineRunRecord) -> None:
        current = self.running()
        if current is not None:
            raise PipelineAlreadyRunningError(current.id)
        self.runs[run.id] = run.model_copy(deep=True)

    def save_run(self, run: PipelineRunRecord) -> None:
        if run.id not in self.runs:
            raise NotFoundError("pipeline run not found", details={"id": str(run.id)})
        self.runs[run.id] = run.model_copy(deep=True)

    def get_run(self, run_id: UUID) -> PipelineRunRecord:
        try:
            return self.runs[run_id].model_copy(deep=True)
        except KeyError:
            raise NotFoundError("pipeline run not found", details={"id": str(run_id)}) from None

    def list_runs(self, limit: int) -> list[PipelineRunRecord]:
        ordered = sorted(self.runs.values(), key=lambda r: r.started_at, reverse=True)
        return [r.model_copy(deep=True) for r in ordered[:limit]]

    def running(self) -> PipelineRunRecord | None:
        found = [r for r in self.runs.values() if r.status is PipelineStatus.RUNNING]
        return found[0].model_copy(deep=True) if found else None
