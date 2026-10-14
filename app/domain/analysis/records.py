"""Persistable Phase 7 records. Analyses are immutable (insert-only)."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Any, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from app.domain.enums import RunStatus
from app.domain.models import Entity, utcnow


class AnalysisStatus(StrEnum):
    SUCCEEDED = "succeeded"  # valid, grounded, catalog-matched analysis stored
    REJECTED_OUTPUT = "rejected_output"  # model answered but failed validation
    FAILED = "failed"  # router/provider error
    BLOCKED = "blocked"  # eligibility refused (not QUALIFIED / no verified contact)
    BUDGET_EXHAUSTED = "budget_exhausted"


class AnalysisRunRecord(Entity):
    status: RunStatus = RunStatus.RUNNING
    mode: str  # mock | live
    policy_version: str
    prompt_version: str
    catalog_version: str
    config: dict[str, Any] = Field(default_factory=dict)
    analyses_created: int = Field(default=0, ge=0)
    succeeded_count: int = Field(default=0, ge=0)
    rejected_count: int = Field(default=0, ge=0)
    failed_count: int = Field(default=0, ge=0)
    blocked_count: int = Field(default=0, ge=0)
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cost_usd: Decimal = Field(default=Decimal(0), ge=0)
    error_code: str | None = None
    error_message: str | None = None
    started_at: AwareDatetime = Field(default_factory=utcnow)
    finished_at: AwareDatetime | None = None


class LeadAnalysisRecord(Entity):
    run_id: UUID
    company_id: UUID
    company_name: str
    qualification_id: UUID | None
    contact_id: UUID | None
    status: AnalysisStatus
    analysis: dict[str, Any] | None = None  # validated LeadAnalysisOutput (json)
    recommended_offer_id: str | None = None
    primary_trigger: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    evidence_bundle: dict[str, Any] = Field(default_factory=dict)  # exactly what was sent
    evidence_signal_ids: list[str] = Field(default_factory=list)
    catalog_version: str
    model: str | None = None
    gateway: str | None = None
    provider: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)
    latency_ms: int | None = Field(default=None, ge=0)
    cost_usd: Decimal | None = Field(default=None, ge=0)
    attempts: int = Field(default=0, ge=0)
    policy_version: str
    prompt_version: str
    error_code: str | None = None
    error_message: str | None = None
    raw_excerpt: str | None = None  # first chars of an unusable answer (debugging)
    analyzed_at: AwareDatetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _invariants(self) -> Self:
        if self.status is AnalysisStatus.SUCCEEDED:
            if self.analysis is None or not self.recommended_offer_id:
                raise ValueError("SUCCEEDED analysis needs the analysis and an offer")
            if self.qualification_id is None or self.contact_id is None:
                raise ValueError("SUCCEEDED analysis needs its qualification and contact")
            if not self.supporting_evidence_ids:
                raise ValueError("SUCCEEDED analysis needs supporting evidence ids")
        if (
            self.status in (AnalysisStatus.FAILED, AnalysisStatus.REJECTED_OUTPUT)
            and not self.error_code
        ):
            raise ValueError("failed / rejected analyses must carry an error_code")
        return self
