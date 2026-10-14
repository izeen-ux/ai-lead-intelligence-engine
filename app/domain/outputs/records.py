"""Persistable export + human-review records (Phase 8). Invariants mirror the DB CHECKs."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from app.domain.enums import RunStatus
from app.domain.models import Entity, utcnow
from app.domain.outputs.sync import RowAction


class ExportMode(StrEnum):
    DRY_RUN = "dry_run"  # plan only, nothing written to any sheet
    MOCK = "mock"  # in-memory workbook (demo / tests)
    LIVE = "live"  # real Google Sheet


class ExportRunRecord(Entity):
    status: RunStatus = RunStatus.RUNNING
    mode: ExportMode
    target: str = "google_sheets"
    spreadsheet_ref: str | None = None  # masked (last 6 chars) - never the full id
    worksheets: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    leads_considered: int = Field(default=0, ge=0)
    inserted_count: int = Field(default=0, ge=0)
    updated_count: int = Field(default=0, ge=0)
    unchanged_count: int = Field(default=0, ge=0)
    withdrawn_count: int = Field(default=0, ge=0)
    skipped_count: int = Field(default=0, ge=0)
    failed_count: int = Field(default=0, ge=0)
    duplicate_rows_found: int = Field(default=0, ge=0)
    api_calls: int = Field(default=0, ge=0)
    error_code: str | None = None
    error_message: str | None = None
    started_at: AwareDatetime = Field(default_factory=utcnow)
    finished_at: AwareDatetime | None = None

    def count(self, action: RowAction) -> None:
        if action is RowAction.INSERT:
            self.inserted_count += 1
        elif action is RowAction.UPDATE:
            self.updated_count += 1
        elif action is RowAction.UNCHANGED:
            self.unchanged_count += 1
        elif action is RowAction.WITHDRAW:
            self.withdrawn_count += 1
        elif action is RowAction.SKIP:
            self.skipped_count += 1
        else:
            self.failed_count += 1

    @property
    def rows_written(self) -> int:
        return self.inserted_count + self.updated_count + self.withdrawn_count


class ExportItemRecord(Entity):
    run_id: UUID
    worksheet: str
    lead_id: str
    company_id: UUID | None = None
    qualification_id: UUID | None = None
    contact_id: UUID | None = None
    analysis_id: UUID | None = None
    action: RowAction
    row_number: int | None = None
    row_hash: str | None = None
    export_status: str | None = None
    error: str | None = None


class ReviewAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REOPEN = "reopen"


class ReviewOutcome(StrEnum):
    APPLIED = "applied"
    REFUSED = "refused"


class HumanReviewRecord(Entity):
    """Audit row for one human REVIEW action. Applied actions point at the NEW decision."""

    company_id: UUID
    action: ReviewAction
    outcome: ReviewOutcome
    reviewer: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=3, max_length=2000)
    prior_decision_id: UUID | None = None
    resulting_decision_id: UUID | None = None
    hard_rules_passed: bool | None = None
    hard_rule_results: list[dict[str, Any]] = Field(default_factory=list)
    refusal_code: str | None = None
    policy_version: str
    reviewed_at: AwareDatetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _invariants(self) -> Self:
        if self.outcome is ReviewOutcome.APPLIED and self.resulting_decision_id is None:
            raise ValueError("an applied review action must create a new decision")
        if self.outcome is ReviewOutcome.REFUSED and not self.refusal_code:
            raise ValueError("a refused review action needs a refusal_code")
        return self
