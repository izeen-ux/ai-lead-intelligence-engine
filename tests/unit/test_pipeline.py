"""Phase 9: pipeline orchestrator - ordering, fail-closed, budgets, duplicate suppression,
stale recovery, cancellation, dry-run safety, no re-spend, scheduler tick, CLI."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.core.config import Settings
from app.core.errors import ConfigurationError, ProviderUnavailableError
from app.demo import CORE_DEMO_PAGES, DemoOutputs, build_demo_outputs, demo_pipeline
from app.domain.analysis.records import AnalysisStatus
from app.domain.models import utcnow
from app.domain.outputs.records import ReviewAction
from app.domain.pipeline import (
    STAGE_ORDER,
    PipelineAlreadyRunningError,
    PipelineStatus,
    Stage,
    StageStatus,
    Trigger,
)
from app.repositories.pipeline import InMemoryPipelineStore
from app.services.pipeline import (
    PipelineService,
    StageContext,
    StageOutcome,
    StageRunner,
    parse_stages,
    run_summary,
    skipped,
)
from app.services.review import ReviewService

ROOT = Path(__file__).resolve().parents[2]
ALL = "discovery,prescreen,website,signals,qualification,enrichment,analysis,export"


def scripted(calls: list[str], **special: Any) -> dict[Stage, StageRunner]:
    """One runner per stage that records the call; ``special[stage]`` = outcome or exception."""

    def make(stage: Stage) -> StageRunner:
        async def runner(ctx: StageContext) -> StageOutcome:
            calls.append(f"{stage.value}:{ctx.limit}:{'dry' if ctx.dry_run else 'write'}")
            step = special.get(stage.value)
            if isinstance(step, BaseException):
                raise step
            return step or StageOutcome(counts={"companies": 1}, stage_run_id=f"{stage.value}-run")

        return runner

    return {s: make(s) for s in STAGE_ORDER}


def service(make_settings, runners: dict[Stage, StageRunner], **kw: Any) -> PipelineService:
    settings = make_settings(pipeline_stages=ALL, **kw.pop("settings", {}))
    return PipelineService(InMemoryPipelineStore(), runners, settings, **kw)


# ================================================================== configuration
def test_parse_stages_orders_and_validates() -> None:
    assert parse_stages("export, analysis ,website") == [
        Stage.WEBSITE,
        Stage.ANALYSIS,
        Stage.EXPORT,
    ]
    with pytest.raises(ConfigurationError, match="unknown pipeline stage"):
        parse_stages("analysis,outreach")
    with pytest.raises(ConfigurationError, match="no pipeline stage"):
        parse_stages(" , ")


def test_default_stages_exclude_paid_discovery() -> None:
    stages = parse_stages(Settings(_env_file=None).pipeline_stages)
    assert Stage.DISCOVERY not in stages and stages[-1] is Stage.EXPORT


# ================================================================== orchestration
async def test_stages_run_in_order_and_are_recorded(make_settings) -> None:
    calls: list[str] = []
    svc = service(make_settings, scripted(calls), allow_mock_writes=True)
    run = await svc.run(trigger=Trigger.CLI, dry_run=False, requested_by="asha")
    assert [c.split(":")[0] for c in calls] == [s.value for s in STAGE_ORDER]
    assert all(c.endswith(":5:write") for c in calls)
    assert run.status is PipelineStatus.SUCCEEDED and run.finished_at is not None
    assert [s.stage for s in run.stages] == list(STAGE_ORDER)
    assert run.result(Stage.ANALYSIS).stage_run_id == "analysis-run"  # type: ignore[union-attr]
    assert run.current_stage is None and (await svc.running()) is None
    stored = (await svc.runs())[0]
    assert stored.id == run.id and stored.requested_by == "asha" and stored.trigger is Trigger.CLI


async def test_failed_stage_stops_the_pipeline(make_settings) -> None:
    calls: list[str] = []
    boom = ProviderUnavailableError("laya", "sidecar down")
    svc = service(make_settings, scripted(calls, qualification=boom))
    run = await svc.run(trigger=Trigger.CLI)
    assert run.status is PipelineStatus.FAILED and run.error_code == "provider_unavailable"
    assert run.error_message == "qualification: sidecar down"
    assert [s.stage for s in run.stages][-1] is Stage.QUALIFICATION
    assert not any(c.startswith(("enrichment", "analysis", "export")) for c in calls)  # fail closed
    assert run.result(Stage.QUALIFICATION).status is StageStatus.FAILED  # type: ignore[union-attr]


async def test_unexpected_crash_is_contained_without_leaking_details(make_settings) -> None:
    secret = RuntimeError("connection string postgresql://user:hunter2@db/x")
    svc = service(make_settings, scripted([], website=secret))
    run = await svc.run(trigger=Trigger.CLI)
    assert run.status is PipelineStatus.FAILED and run.error_code == "internal_error"
    assert "hunter2" not in str(run_summary(run))


async def test_pipeline_cost_ceiling(make_settings) -> None:
    calls: list[str] = []
    pricey = StageOutcome(estimated_cost_usd=Decimal("0.60"))
    svc = service(
        make_settings, scripted(calls, enrichment=pricey, analysis=pricey),
        settings={"pipeline_max_estimated_cost_usd_per_run": Decimal("1.00")},
    )  # fmt: skip
    run = await svc.run(trigger=Trigger.CLI)
    assert run.status is PipelineStatus.FAILED and run.error_code == "pipeline_budget_exhausted"
    assert run.estimated_cost_usd == Decimal("1.20")
    assert not any(c.startswith("export") for c in calls)


async def test_default_limit_comes_from_configuration(make_settings) -> None:
    calls: list[str] = []
    svc = service(make_settings, scripted(calls), settings={"pipeline_company_limit": 3})
    run = await svc.run(trigger=Trigger.CLI, stages=[Stage.ANALYSIS])
    assert run.company_limit == 3 and calls == ["analysis:3:dry"]


async def test_explicit_limit_is_honoured_above_the_default(make_settings) -> None:
    """Regression: ``--limit 10`` used to be silently reduced to PIPELINE_COMPANY_LIMIT (5)."""
    calls: list[str] = []
    svc = service(make_settings, scripted(calls))
    run = await svc.run(trigger=Trigger.CLI, limit=10, stages=[Stage.ANALYSIS])
    assert run.company_limit == 10 and calls == ["analysis:10:dry"]


def test_limit_above_the_ceiling_is_refused_not_reduced(make_settings) -> None:
    svc = service(make_settings, scripted([]), settings={"pipeline_max_company_limit": 12})
    with pytest.raises(ConfigurationError, match="PIPELINE_MAX_COMPANY_LIMIT"):
        svc.new_run(trigger=Trigger.CLI, limit=13)


def test_stages_all_includes_discovery() -> None:
    assert parse_stages("all") == list(STAGE_ORDER)


async def test_missing_runner_is_skipped_not_failed(make_settings) -> None:
    svc = service(make_settings, {})
    run = await svc.run(trigger=Trigger.CLI, stages=[Stage.WEBSITE])
    assert run.status is PipelineStatus.SUCCEEDED
    assert run.stages[0].status is StageStatus.SKIPPED


# ================================================================== safety
async def test_writing_run_needs_live_mode(make_settings) -> None:
    mock = service(make_settings, scripted([]))
    with pytest.raises(ConfigurationError, match="PROVIDER_MODE=live"):
        mock.new_run(trigger=Trigger.API, dry_run=False)
    assert mock.new_run(trigger=Trigger.API).dry_run is True  # dry run is the default
    live = service(make_settings, scripted([]), settings={"provider_mode": "live"})
    assert live.new_run(trigger=Trigger.API, dry_run=False).mode == "live"


async def test_duplicate_trigger_is_refused(make_settings) -> None:
    svc = service(make_settings, scripted([]))
    first = svc.new_run(trigger=Trigger.API)
    await svc.start(first)
    with pytest.raises(PipelineAlreadyRunningError) as exc:
        await svc.start(svc.new_run(trigger=Trigger.SCHEDULE))
    assert exc.value.running_id == first.id
    await svc.execute(first)
    await svc.run(trigger=Trigger.CLI)  # slot is free again


async def test_stale_run_recovery(make_settings) -> None:
    svc = service(make_settings, scripted([]), settings={"pipeline_stale_after_minutes": 30})
    run = svc.new_run(trigger=Trigger.SCHEDULE)
    await svc.start(run)
    assert await svc.recover_stale() == []  # fresh heartbeat: left alone
    run.heartbeat_at = utcnow() - timedelta(minutes=31)
    svc._store.save_run(run)
    [recovered] = await svc.recover_stale()
    assert recovered.status is PipelineStatus.ABANDONED and recovered.error_code == "stale_run"
    assert "stage: discovery" in (recovered.error_message or "")  # the stage it died in
    assert (await svc.running()) is None and await svc.recover_stale() == []
    await svc.run(trigger=Trigger.CLI)  # a new run can start


async def test_cancellation_is_recorded(make_settings) -> None:
    svc = service(make_settings, scripted([], signals=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await svc.run(trigger=Trigger.SCHEDULE)
    [run] = await svc.runs()
    assert run.status is PipelineStatus.CANCELLED and run.error_code == "cancelled"
    assert (await svc.running()) is None


async def test_logs_carry_run_id_stage_status_and_duration(make_settings, caplog) -> None:
    caplog.set_level(logging.INFO)
    svc = service(make_settings, scripted([]))
    run = await svc.run(trigger=Trigger.CLI, stages=[Stage.ANALYSIS])
    stage = next(r for r in caplog.records if r.getMessage() == "pipeline stage finished")
    fields = stage.__dict__
    assert fields["pipeline_run_id"] == str(run.id) and fields["stage"] == "analysis"
    assert fields["status"] == "succeeded" and fields["duration_ms"] >= 0
    done = next(r for r in caplog.records if r.getMessage() == "pipeline run finished")
    assert done.__dict__["status"] == "succeeded"
    assert done.__dict__["estimated_cost_usd"] == "0"


# ================================================================== real stage services (demo)
@pytest.fixture
async def demo(make_settings) -> tuple[Settings, DemoOutputs]:
    settings = make_settings()
    return settings, await build_demo_outputs(settings, dict(CORE_DEMO_PAGES))


def snapshot(demo: DemoOutputs) -> tuple[int, int, int]:
    w = demo.world
    return (
        len(w.qualifications.decisions),
        len(w.enrichment.contact_log),
        len(demo.analyses.records),
    )


async def test_dry_run_writes_nothing(demo) -> None:
    settings, d = demo
    before = snapshot(d)
    run = await demo_pipeline(settings, d).run(trigger=Trigger.CLI)
    assert run.status is PipelineStatus.SUCCEEDED and run.dry_run
    assert snapshot(d) == before and d.outputs.runs == {}
    export = run.result(Stage.EXPORT)
    assert (
        export is not None and export.counts["leads"] == 3 and "plan only" in (export.detail or "")
    )


async def test_a_small_company_limit_does_not_hide_leads_from_the_sheet(demo) -> None:
    """Regression: "companies per step" = 1 exported 1 lead per kind, the rest never got a row."""
    settings, d = demo
    run = await demo_pipeline(settings, d).run(trigger=Trigger.CLI, limit=1, stages=[Stage.EXPORT])
    export = run.result(Stage.EXPORT)
    assert export is not None and export.counts["leads"] == 3


async def test_approved_lead_flows_through_gates_once(demo) -> None:
    settings, d = demo
    drivnly = next(
        c for c in d.world.company_ids if d.world.enrichment.load_company(c).name == "Drivnly"
    )
    pipeline = demo_pipeline(settings, d)
    idle = await pipeline.run(trigger=Trigger.CLI, dry_run=False)
    assert idle.result(Stage.ENRICHMENT).status is StageStatus.SKIPPED  # type: ignore[union-attr]
    await ReviewService(d.world.qualifications, d.outputs, settings).act(
        drivnly, ReviewAction.APPROVE, reviewer="Asha", reason="evidence checked"
    )
    run = await pipeline.run(trigger=Trigger.CLI, dry_run=False)
    assert run.status is PipelineStatus.SUCCEEDED
    assert run.result(Stage.ENRICHMENT).counts["verified"] == 1  # type: ignore[union-attr]
    analysis = run.result(Stage.ANALYSIS)
    assert analysis is not None and analysis.counts["succeeded"] == 1
    done = [r for r in d.analyses.analyses(drivnly) if r.status is AnalysisStatus.SUCCEEDED]
    assert len(done) == 1 and done[0].provider == "mock"
    again = await pipeline.run(trigger=Trigger.SCHEDULE, dry_run=False)  # nothing left to do
    assert {s.status for s in again.stages if s.stage is not Stage.EXPORT} == {StageStatus.SKIPPED}
    after = [r for r in d.analyses.analyses(drivnly) if r.status is AnalysisStatus.SUCCEEDED]
    assert len(after) == 1  # no re-analysis, no re-spend


def test_skipped_helper() -> None:
    assert skipped("why").status is StageStatus.SKIPPED and skipped("why").detail == "why"


# ================================================================== scheduler + CLI
def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_scheduler_tick(make_settings) -> None:
    scheduler = load("run_scheduler")
    settings = make_settings(pipeline_stages=ALL)
    calls: list[str] = []
    svc = PipelineService(InMemoryPipelineStore(), scripted(calls), settings)
    assert await scheduler.tick(svc, settings, due=False) == "idle" and calls == []
    assert await scheduler.tick(svc, settings, due=True) == "ran:succeeded"
    [run] = await svc.runs()
    assert run.trigger is Trigger.SCHEDULE and run.dry_run  # mock mode -> never a writing run
    await svc.start(svc.new_run(trigger=Trigger.API))
    assert await scheduler.tick(svc, settings, due=True) == "skipped_running"


@pytest.fixture
def _cwd(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    for var in ("PROVIDER_MODE", "ALLOW_PAID_PROVIDER_CALLS", "DATABASE_URL", "PIPELINE_STAGES"):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.usefixtures("_cwd")
def test_cli_default_is_an_offline_dry_run(capsys) -> None:
    assert load("run_pipeline").main([]) == 0
    out = capsys.readouterr().out
    assert "mode: DRY RUN | data: DEMO (synthetic, in-memory) | providers: mock" in out
    assert "status: SUCCEEDED" in out and "DRY RUN (nothing written)" in out
    assert "paid calls: blocked | sheet writes: blocked" in out


@pytest.mark.usefixtures("_cwd")
@pytest.mark.parametrize(
    ("argv", "problem"),
    [
        (["--live"], "--live / --status / --recover require --db"),
        (["--status"], "--live / --status / --recover require --db"),
        (["--db", "--live"], "--live requires PROVIDER_MODE=live"),
        (["--db"], "database schema not ready"),
        (["--limit", "0"], "--limit must be 1..25"),
        (["--limit", "26"], "PIPELINE_MAX_COMPANY_LIMIT"),
    ],
)
def test_cli_refusals(capsys, argv, problem) -> None:
    assert load("run_pipeline").main(argv) == 2
    assert problem in capsys.readouterr().out


@pytest.mark.usefixtures("_cwd")
def test_cli_unknown_stage(capsys) -> None:
    assert load("run_pipeline").main(["--stages", "outreach"]) == 1
    assert "unknown pipeline stage" in capsys.readouterr().err
