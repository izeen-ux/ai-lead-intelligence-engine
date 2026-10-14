"""Local dashboard + operator API (Phase 8-10).

Reads go through ``DashboardService``; writes are (a) a human REVIEW action through
``ReviewService`` and (b) pipeline control through ``PipelineService`` (trigger a run, recover
stale runs). The API layer itself calls no provider.

Access (see ``app/api/security.py``): every ``/api/v1`` route needs the operator token when one
is configured (mandatory in staging/production); every POST also needs
``X-Dashboard-Request: 1``. The static page is open and holds no data.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.api.deps import get_app_settings, get_dashboard_backend, get_registry
from app.api.security import require_operator, require_write_header
from app.core.config import Settings
from app.core.errors import ConfigurationError
from app.domain.enums import QualificationDecision
from app.domain.outputs.leads import iso
from app.domain.outputs.records import ReviewAction
from app.domain.pipeline import PipelineAlreadyRunningError, Stage, Trigger
from app.providers.registry import ProviderRegistry
from app.services.credits import CreditService
from app.services.dashboard import DashboardBackend, DashboardService
from app.services.pipeline import PipelineService, owner_names, run_summary
from app.services.review import ReviewService

PAGE = Path(__file__).resolve().parents[2] / "web" / "dashboard.html"
page_router = APIRouter(tags=["dashboard"])
router = APIRouter(
    prefix="/api/v1", tags=["operator-api"], dependencies=[Depends(require_operator)]
)
WRITE = [Depends(require_write_header)]


def get_dashboard(
    settings: Settings = Depends(get_app_settings),
    registry: ProviderRegistry = Depends(get_registry),
    backend: DashboardBackend = Depends(get_dashboard_backend),
) -> DashboardService:
    return DashboardService(backend, registry.statuses, settings)


def get_pipeline(backend: DashboardBackend = Depends(get_dashboard_backend)) -> PipelineService:
    if backend.pipeline is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "pipeline control unavailable")
    return backend.pipeline


class ReviewRequest(BaseModel):
    action: Literal["approve", "reject", "reopen"]
    reviewer: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=3, max_length=2000)
    expected_decision_id: UUID | None = None


class PipelineRunRequest(BaseModel):
    dry_run: bool = True  # a writing run must be asked for explicitly
    stages: list[Stage] | None = Field(default=None, max_length=8)
    limit: int | None = Field(default=None, ge=1, le=50)
    requested_by: str = Field(min_length=1, max_length=120)
    owner: str | None = Field(default=None, max_length=120)  # who gets the leads of this run
    prompt: str | None = Field(default=None, max_length=2000)  # search queries, one per line
    target_leads: int | None = Field(default=None, ge=1, le=50)  # repeat until this many leads


@page_router.get("/dashboard", include_in_schema=False)
def dashboard_page() -> FileResponse:
    return FileResponse(PAGE, media_type="text/html")


@router.get("/dashboard/overview")
async def overview(dashboard: DashboardService = Depends(get_dashboard)) -> dict[str, Any]:
    return await dashboard.overview()


@router.get("/credits")
async def credits(
    settings: Settings = Depends(get_app_settings),
    backend: DashboardBackend = Depends(get_dashboard_backend),
    registry: ProviderRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Credits used / remaining per provider + what the pre-screen saved. In live mode this
    reads the providers' own FREE account counters; it never starts a paid operation."""
    usage = getattr(registry.enrichment, "account_usage", None)  # live sidecar only
    recent = [run_summary(r) for r in await backend.pipeline.runs(15)] if backend.pipeline else []
    return await CreditService(settings, backend.outputs, hunter_usage=usage).report(recent)


@router.get("/owners")
def owners(settings: Settings = Depends(get_app_settings)) -> dict[str, Any]:
    return {"owners": owner_names(settings.lead_owners)}


@router.get("/leads")
async def leads(
    decision: QualificationDecision | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100_000),
    dashboard: DashboardService = Depends(get_dashboard),
) -> dict[str, Any]:
    items = await dashboard.leads(decision, limit, offset)
    return {"items": items, "limit": limit, "offset": offset}


@router.get("/review-queue")
async def review_queue(
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100_000),
    dashboard: DashboardService = Depends(get_dashboard),
) -> dict[str, Any]:
    items = await dashboard.review_queue(limit, offset)
    return {"items": items, "limit": limit, "offset": offset}


@router.get("/leads/{company_id}")
async def lead_detail(
    company_id: UUID, dashboard: DashboardService = Depends(get_dashboard)
) -> dict[str, Any]:
    return await dashboard.lead_detail(company_id)


@router.post("/review/{company_id}", dependencies=WRITE)
async def review_action(
    company_id: UUID,
    body: ReviewRequest,
    response: Response,
    settings: Settings = Depends(get_app_settings),
    backend: DashboardBackend = Depends(get_dashboard_backend),
) -> dict[str, Any]:
    service = ReviewService(backend.qualifications, backend.outputs, settings)
    result = await service.act(
        company_id,
        ReviewAction(body.action),
        reviewer=body.reviewer,
        reason=body.reason,
        expected_decision_id=body.expected_decision_id,
    )
    if not result.applied:
        response.status_code = status.HTTP_409_CONFLICT
    a = result.audit
    return {
        "applied": result.applied,
        "outcome": a.outcome.value,
        "refusal_code": a.refusal_code,
        "audit_id": str(a.id),
        "reviewed_at": iso(a.reviewed_at),
        "hard_rules_passed": a.hard_rules_passed,
        "new_decision": None
        if result.decision is None
        else {"id": str(result.decision.id), "decision": result.decision.decision.value},
    }


# ---------------------------------------------------------------------- pipeline control
@router.get("/pipeline/runs")
async def pipeline_runs(
    limit: int = Query(default=20, ge=1, le=100),
    pipeline: PipelineService = Depends(get_pipeline),
) -> dict[str, Any]:
    return {"items": [run_summary(r) for r in await pipeline.runs(limit)]}


@router.get("/pipeline/runs/{run_id}")
async def pipeline_run(
    run_id: UUID, pipeline: PipelineService = Depends(get_pipeline)
) -> dict[str, Any]:
    runs = [r for r in await pipeline.runs(100) if r.id == run_id]
    if not runs:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "pipeline run not found")
    return run_summary(runs[0])


@router.post("/pipeline/runs", status_code=status.HTTP_202_ACCEPTED, dependencies=WRITE)
async def trigger_pipeline(
    body: PipelineRunRequest,
    request: Request,
    pipeline: PipelineService = Depends(get_pipeline),
) -> dict[str, Any]:
    """Start one pipeline run in the background. Dry run unless ``dry_run`` is false; a
    writing run is refused unless this process runs in live provider mode."""
    try:
        run = pipeline.new_run(
            trigger=Trigger.API,
            dry_run=body.dry_run,
            stages=body.stages,
            limit=body.limit,
            requested_by=body.requested_by,
            owner=body.owner,
            prompt=body.prompt,
            target_leads=body.target_leads,
        )
        await pipeline.start(run)
    except ConfigurationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.message) from None
    except PipelineAlreadyRunningError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"a pipeline run is already in progress ({exc.running_id})"
        ) from None
    tasks: set[asyncio.Task[Any]] = request.app.state.pipeline_tasks
    task = asyncio.create_task(pipeline.execute(run))
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return {"run_id": str(run.id), "status": run.status.value, "dry_run": run.dry_run}


@router.post("/pipeline/recover", dependencies=WRITE)
async def recover_pipeline(pipeline: PipelineService = Depends(get_pipeline)) -> dict[str, Any]:
    recovered = await pipeline.recover_stale()
    return {"recovered": [run_summary(r) for r in recovered]}
