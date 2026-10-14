"""Pipeline orchestration records (Phase 9, minimal).

One pipeline run = the existing stage services executed in a fixed order, each through its own
gate and budget. The orchestrator adds no business rule: it only sequences, limits, records
and recovers. Stage histories (qualification decisions, contacts, analyses, exports) stay in
their own append-only tables; a pipeline run row links to them by stage run id.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import AwareDatetime, Field

from app.domain.models import DomainModel, Entity, utcnow

PIPELINE_POLICY_VERSION = "p9-2026.10.05"


class Stage(StrEnum):
    DISCOVERY = "discovery"
    PRESCREEN = "prescreen"  # free scrape / skip verdict before paid website intelligence
    WEBSITE = "website"
    SIGNALS = "signals"
    QUALIFICATION = "qualification"
    ENRICHMENT = "enrichment"
    ANALYSIS = "analysis"
    EXPORT = "export"


STAGE_ORDER: tuple[Stage, ...] = tuple(Stage)


class StageStatus(StrEnum):
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"  # not applicable in this mode / nothing configured (not an error)
    FAILED = "failed"  # stops the pipeline: later stages are not run


class PipelineStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"  # stopped by the operator / process shutdown
    ABANDONED = "abandoned"  # found stale (no heartbeat) and closed by recovery


class Trigger(StrEnum):
    CLI = "cli"
    API = "api"
    SCHEDULE = "schedule"


class StageResult(DomainModel):
    stage: Stage
    status: StageStatus
    started_at: AwareDatetime = Field(default_factory=utcnow)
    finished_at: AwareDatetime = Field(default_factory=utcnow)
    duration_ms: int = Field(default=0, ge=0)
    counts: dict[str, int] = Field(default_factory=dict)
    estimated_cost_usd: Decimal = Field(default=Decimal(0), ge=0)
    stage_run_id: str | None = None  # id of the stage's own run row, when it has one
    detail: str | None = None  # why skipped / short note
    error_code: str | None = None
    error_message: str | None = None


class PipelineRunRecord(Entity):
    status: PipelineStatus = PipelineStatus.RUNNING
    trigger: Trigger
    requested_by: str | None = Field(default=None, max_length=120)
    mode: str  # mock | live (provider mode of the process that ran it)
    dry_run: bool  # True = nothing written by any stage
    stages_requested: list[Stage]
    company_limit: int = Field(ge=1)
    stages: list[StageResult] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    estimated_cost_usd: Decimal = Field(default=Decimal(0), ge=0)
    error_code: str | None = None
    error_message: str | None = None
    started_at: AwareDatetime = Field(default_factory=utcnow)
    heartbeat_at: AwareDatetime = Field(default_factory=utcnow)
    finished_at: AwareDatetime | None = None

    def result(self, stage: Stage) -> StageResult | None:
        return next((s for s in self.stages if s.stage is stage), None)

    @property
    def current_stage(self) -> Stage | None:
        """The stage a RUNNING run is in (the first requested stage without a result)."""
        if self.status is not PipelineStatus.RUNNING:
            return None
        now = self.config.get("stage_now")  # set by the orchestrator (rounds repeat stages)
        if now in {s.value for s in self.stages_requested}:
            return Stage(now)
        done = {s.stage for s in self.stages}
        return next((s for s in self.stages_requested if s not in done), None)


class PipelineAlreadyRunningError(RuntimeError):
    """Another pipeline run holds the single running slot (duplicate trigger suppressed)."""

    def __init__(self, running_id: UUID | None = None) -> None:
        super().__init__("a pipeline run is already in progress")
        self.running_id = running_id
