"""Phase 9 persistence on real PostgreSQL: run rows, the single running slot, candidate
queries (no re-deciding human decisions, no re-enrichment, no re-analysis)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import IntegrityError

from app.domain.enums import SignalType
from app.domain.outputs.records import ReviewAction
from app.domain.pipeline import (
    PipelineAlreadyRunningError,
    PipelineStatus,
    Stage,
    StageStatus,
    Trigger,
)
from app.providers.registry import ProviderRegistry
from app.repositories.analysis import SqlAnalysisStore
from app.repositories.enrichment import SqlEnrichmentStore
from app.repositories.outputs import SqlOutputStore
from app.repositories.pipeline import SqlPipelineStore
from app.repositories.qualification import SqlQualificationStore
from app.services.pipeline import PipelineService
from app.services.pipeline_stages import core_stages, sql_stages
from app.services.review import ReviewService
from tests.integration.test_discovery_postgres import pg_url  # noqa: F401 (fixture)
from tests.integration.test_enrichment_postgres import add_team_page
from tests.integration.test_outputs_postgres import TABLES as OUTPUT_TABLES
from tests.integration.test_qualification_postgres import add_signals
from tests.integration.test_signal_research_postgres import make_verified

pytestmark = pytest.mark.postgres
TABLES = ("pipeline_runs", *OUTPUT_TABLES)


@pytest.fixture
def engine(pg_url: str) -> Iterator[Engine]:  # noqa: F811
    eng = create_engine(pg_url)
    with eng.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)} CASCADE"))
    yield eng
    eng.dispose()


def researched_company(engine: Engine) -> uuid.UUID:
    """A VERIFIED company with a qualifying funding signal + team page, signals researched."""
    cid = make_verified(engine)
    add_signals(engine, cid, [("Drivn raises Rs 4 crore seed round", SignalType.FUNDING, 10)])
    add_team_page(engine, cid)
    with engine.begin() as conn:
        conn.execute(
            text(
                "update companies set signals_researched_at = now() - interval '1 minute' "
                "where id = :i"
            ),
            {"i": cid},
        )
    return cid


def writing_pipeline(engine: Engine, make_settings) -> PipelineService:
    """Real stage services over PostgreSQL with MOCK providers (writes allowed for the test)."""
    settings = make_settings()
    store = SqlPipelineStore(engine)
    runners = core_stages(
        settings,
        ProviderRegistry(settings),
        qualifications=SqlQualificationStore(engine),
        enrichment=SqlEnrichmentStore(engine),
        analyses=SqlAnalysisStore(engine),
        outputs=SqlOutputStore(engine),
        qualification_candidates=store.qualification_candidates,
        enrichment_candidates=store.enrichment_candidates,
        analysis_candidates=store.analysis_candidates,
    )
    return PipelineService(store, runners, settings, allow_mock_writes=True)


def count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(f"select count(*) from {table}")).scalar_one())  # noqa: S608


async def test_run_rows_and_single_running_slot(engine, make_settings) -> None:
    store = SqlPipelineStore(engine)
    svc = PipelineService(store, {}, make_settings())
    first = svc.new_run(trigger=Trigger.API, requested_by="asha")
    await svc.start(first)
    assert store.running() is not None and store.get_run(first.id).requested_by == "asha"
    with pytest.raises(PipelineAlreadyRunningError) as exc:
        await svc.start(svc.new_run(trigger=Trigger.SCHEDULE))
    assert exc.value.running_id == first.id
    done = await svc.execute(first)
    assert done.status is PipelineStatus.SUCCEEDED and store.running() is None
    second = await svc.run(trigger=Trigger.CLI)
    assert [r.id for r in store.list_runs(5)] == [second.id, first.id]
    with pytest.raises(IntegrityError, match="slot_matches_status"), engine.begin() as conn:
        conn.execute(
            text("update pipeline_runs set active_slot = 1 where id = :i"), {"i": first.id}
        )


async def test_pipeline_moves_a_company_through_every_gate_once(engine, make_settings) -> None:
    cid = researched_company(engine)
    store = SqlPipelineStore(engine)
    assert store.qualification_candidates(5) == [cid]
    assert store.enrichment_candidates(5) == store.analysis_candidates(5) == []
    pipeline = writing_pipeline(engine, make_settings)
    stages = [Stage.QUALIFICATION, Stage.ENRICHMENT, Stage.ANALYSIS, Stage.EXPORT]
    run = await pipeline.run(trigger=Trigger.CLI, dry_run=False, stages=stages)
    assert run.status is PipelineStatus.SUCCEEDED
    assert run.result(Stage.QUALIFICATION).counts["qualified"] == 1  # type: ignore[union-attr]
    assert run.result(Stage.ENRICHMENT).counts["verified"] == 1  # type: ignore[union-attr]
    assert run.result(Stage.ANALYSIS).counts["succeeded"] == 1  # type: ignore[union-attr]
    stored = store.get_run(run.id)
    assert [s.stage for s in stored.stages] == stages
    assert stored.result(Stage.ANALYSIS).stage_run_id is not None  # type: ignore[union-attr]
    rows = {t: count(engine, t) for t in
            ("qualification_decisions", "enrichment_contacts", "lead_analyses")}  # fmt: skip
    assert rows == {"qualification_decisions": 1, "enrichment_contacts": 1, "lead_analyses": 1}
    # everything is done: no candidate left, a second run re-does (and re-pays) nothing
    assert store.qualification_candidates(5) == store.enrichment_candidates(5) == []
    assert store.analysis_candidates(5) == []
    again = await pipeline.run(trigger=Trigger.SCHEDULE, dry_run=False, stages=stages)
    skipped = {s.stage for s in again.stages if s.status is StageStatus.SKIPPED}
    assert skipped == {Stage.QUALIFICATION, Stage.ENRICHMENT, Stage.ANALYSIS}
    assert {t: count(engine, t) for t in rows} == rows


async def test_dry_run_over_postgres_writes_no_stage_data(engine, make_settings) -> None:
    researched_company(engine)
    settings = make_settings()
    pipeline = PipelineService(
        SqlPipelineStore(engine), sql_stages(settings, engine, ProviderRegistry(settings)), settings
    )
    run = await pipeline.run(trigger=Trigger.CLI)
    assert run.status is PipelineStatus.SUCCEEDED and run.dry_run
    assert run.result(Stage.WEBSITE).status is StageStatus.SKIPPED  # type: ignore[union-attr]
    assert run.result(Stage.QUALIFICATION).counts["companies"] == 1  # type: ignore[union-attr]
    for table in ("qualification_decisions", "enrichment_contacts", "lead_analyses", "export_runs"):
        assert count(engine, table) == 0
    assert count(engine, "pipeline_runs") == 1  # only the run itself is recorded


async def test_human_decisions_are_never_re_decided(engine, make_settings) -> None:
    cid = researched_company(engine)
    pipeline = writing_pipeline(engine, make_settings)
    await pipeline.run(trigger=Trigger.CLI, dry_run=False, stages=[Stage.QUALIFICATION])
    review = ReviewService(SqlQualificationStore(engine), SqlOutputStore(engine), make_settings())
    result = await review.act(cid, ReviewAction.REOPEN, reviewer="Asha", reason="recheck please")
    assert result.applied
    with engine.begin() as conn:  # newer signals arrive after the human decision
        conn.execute(text("update companies set signals_researched_at = now() + interval '1 hour'"))
    store = SqlPipelineStore(engine)
    assert store.qualification_candidates(5) == []  # the human REVIEW stays until a human acts
    assert store.signal_candidates(5) == []  # already researched
    assert count(engine, "qualification_decisions") == 2


async def test_decision_made_without_laya_is_retried_by_the_next_run(engine, make_settings) -> None:
    """Regression (live run 2026-10-05): Laya timed out, the company got a fail-closed REVIEW
    and no later pipeline run ever evaluated it again."""
    from app.core.errors import ProviderTimeoutError
    from app.domain.enums import QualificationDecision
    from app.providers.laya import MockLayaProvider
    from app.services.qualification import QualificationService

    class TimingOutLaya(MockLayaProvider):
        async def evaluate(self, request):
            raise ProviderTimeoutError("laya", "Laya sidecar did not answer within 30s")

    cid = researched_company(engine)
    settings = make_settings()
    qstore, store = SqlQualificationStore(engine), SqlPipelineStore(engine)
    _, [provisional] = await QualificationService(qstore, TimingOutLaya(settings), settings).run(
        [cid]
    )
    assert provisional.decision is QualificationDecision.REVIEW
    assert provisional.laya_status.value == "error"
    assert store.qualification_candidates(5) == [cid]  # provisional -> evaluated again

    pipeline = writing_pipeline(engine, make_settings)
    await pipeline.run(trigger=Trigger.CLI, dry_run=False, stages=[Stage.QUALIFICATION])
    latest = qstore.latest_decision(cid)
    assert latest is not None and latest.id != provisional.id
    assert latest.laya_status.value == "ok" and latest.decision is QualificationDecision.QUALIFIED
    assert store.qualification_candidates(5) == []  # a real decision is not repeated
    assert count(engine, "qualification_decisions") == 2  # history kept, nothing overwritten
