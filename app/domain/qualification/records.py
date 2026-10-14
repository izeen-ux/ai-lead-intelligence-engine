"""Persistable qualification records (Phase 5). Invariants are enforced here AND in the DB."""

from __future__ import annotations

from typing import Any, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from app.domain.enums import QualificationDecision, QualificationPath, RunStatus, StageStatus
from app.domain.models import Entity, utcnow


class QualificationRunRecord(Entity):
    status: RunStatus = RunStatus.RUNNING
    policy_version: str
    laya_mode: str  # mock | live
    llm_review_mode: str  # disabled | mock | live
    icp_config: dict[str, Any] = Field(default_factory=dict)
    companies_evaluated: int = Field(default=0, ge=0)
    qualified_count: int = Field(default=0, ge=0)
    review_count: int = Field(default=0, ge=0)
    not_qualified_count: int = Field(default=0, ge=0)
    error_code: str | None = None
    error_message: str | None = None
    started_at: AwareDatetime = Field(default_factory=utcnow)
    finished_at: AwareDatetime | None = None

    def count(self, decision: QualificationDecision) -> None:
        self.companies_evaluated += 1
        if decision is QualificationDecision.QUALIFIED:
            self.qualified_count += 1
        elif decision is QualificationDecision.REVIEW:
            self.review_count += 1
        else:
            self.not_qualified_count += 1


class QualificationDecisionRecord(Entity):
    """One immutable decision. ``evidence_signal_ids`` are the persisted Phase 4 signals the
    decision relied on (provenance); ``evidence_bundle`` is exactly what Laya/LLM saw."""

    run_id: UUID
    company_id: UUID
    company_name: str
    decision: QualificationDecision
    path: QualificationPath
    hard_rules_passed: bool
    hard_rule_results: list[dict[str, Any]] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    laya_status: StageStatus = StageStatus.SKIPPED
    laya_result: dict[str, Any] | None = None
    laya_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    laya_model_version: str | None = None
    llm_review_status: StageStatus = StageStatus.SKIPPED
    llm_review: dict[str, Any] | None = None
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    policy_version: str
    icp_config: dict[str, Any] = Field(default_factory=dict)
    evidence_bundle: dict[str, Any] = Field(default_factory=dict)
    evidence_signal_ids: list[UUID] = Field(default_factory=list)
    signal_count: int = Field(default=0, ge=0)
    event_count: int = Field(default=0, ge=0)
    decided_at: AwareDatetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _gate_invariants(self) -> Self:
        if self.decision is QualificationDecision.QUALIFIED:
            if not self.hard_rules_passed:
                raise ValueError("QUALIFIED requires passed hard rules")
            if self.path is QualificationPath.NONE:
                raise ValueError("QUALIFIED requires a qualification path")
        return self

    @property
    def enrichment_eligible(self) -> bool:
        """The ONLY condition under which Phase 6 may enrich this company."""
        return self.decision is QualificationDecision.QUALIFIED and self.hard_rules_passed
