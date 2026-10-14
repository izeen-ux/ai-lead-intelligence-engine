"""Phase 8 persistence on real PostgreSQL (separate lead_intel_test database)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.domain.enums import QualificationDecision, RunStatus
from app.domain.models import utcnow
from app.domain.outputs.leads import LEADS_COLUMNS, lead_id_for
from app.domain.outputs.records import ExportMode, ReviewAction, ReviewOutcome
from app.domain.outputs.sync import RowAction
from app.providers.ai_router import MockAiRouterProvider
from app.providers.google_sheets import InMemorySheetsWriter
from app.repositories.analysis import SqlAnalysisStore
from app.repositories.enrichment import SqlEnrichmentStore
from app.repositories.outputs import SqlOutputStore
from app.repositories.qualification import SqlQualificationStore
from app.services.lead_analysis import LeadAnalysisService
from app.services.leads import LeadViewService
from app.services.review import ReviewService
from app.services.sheets_export import SheetsExportService
from tests.integration.test_discovery_postgres import pg_url  # noqa: F401 (fixture)
from tests.integration.test_enrichment_postgres import qualified_drivn
from tests.integration.test_enrichment_postgres import service as enrichment_service
from tests.integration.test_lead_analysis_postgres import TABLES as ANALYSIS_TABLES

pytestmark = pytest.mark.postgres

TABLES = ("export_items", "export_runs", "human_reviews", *ANALYSIS_TABLES)


@pytest.fixture
def engine(pg_url: str) -> Iterator[Engine]:  # noqa: F811
    eng = create_engine(pg_url)
    with eng.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)} CASCADE"))
    yield eng
    eng.dispose()


def views(engine: Engine) -> LeadViewService:
    return LeadViewService(
        SqlQualificationStore(engine), SqlEnrichmentStore(engine), SqlAnalysisStore(engine)
    )


async def ready_lead(engine: Engine, make_settings) -> uuid.UUID:
    """Drivn: QUALIFIED (Phase 5) -> VERIFIED contact (Phase 6) -> analysis (Phase 7), all mocks."""
    cid = await qualified_drivn(engine, make_settings)
    await enrichment_service(engine, make_settings).run()
    settings = make_settings()
    await LeadAnalysisService(
        SqlAnalysisStore(engine), SqlQualificationStore(engine), SqlEnrichmentStore(engine),
        MockAiRouterProvider(settings), settings,
    ).run()  # fmt: skip
    return cid


def exporter(engine: Engine, make_settings, sheet: InMemorySheetsWriter) -> SheetsExportService:
    return SheetsExportService(
        SqlOutputStore(engine), views(engine), make_settings(), sheet, mode=ExportMode.LIVE
    )


async def test_lead_view_from_postgres_is_ready_and_traceable(engine, make_settings) -> None:
    cid = await ready_lead(engine, make_settings)
    store = SqlOutputStore(engine)
    assert store.company_ids_by_decision(QualificationDecision.QUALIFIED, 10) == [cid]
    assert store.resolve_lead_ids([lead_id_for(cid), "lead_0000000000000000"]) == {
        lead_id_for(cid): cid
    }
    v = views(engine).view(cid)
    assert v is not None and v.export_status.value == "ready"
    assert v.contact is not None and v.analysis is not None
    assert v.analysis.qualification_id == v.decision.id == v.contact.qualification_id


async def test_export_history_is_persisted_and_idempotent(engine, make_settings) -> None:
    cid = await ready_lead(engine, make_settings)
    sheet = InMemorySheetsWriter()
    first = (await exporter(engine, make_settings, sheet).run()).run
    second = (await exporter(engine, make_settings, sheet).run()).run
    assert (first.inserted_count, second.unchanged_count, second.rows_written) == (1, 1, 0)
    store = SqlOutputStore(engine)
    runs = store.export_runs(5)
    assert [r.id for r in runs] == [second.id, first.id]
    assert runs[0].status is RunStatus.SUCCEEDED and runs[0].finished_at is not None
    [item] = store.export_items(first.id)
    assert (item.action, item.row_number, item.company_id) == (RowAction.INSERT, 2, cid)
    assert item.qualification_id and item.contact_id and item.analysis_id
    assert store.last_export_item(lead_id_for(cid)) is not None
    counts = store.pipeline_counts()
    assert (counts.qualified, counts.enriched, counts.analyzed, counts.exported) == (1, 1, 1, 1)
    assert sheet.tabs["Ai Leads"][1][LEADS_COLUMNS.index("lead_id")] == lead_id_for(cid)
    lines = store.usage_summary(utcnow() - timedelta(days=1))
    assert {(u.provider, u.metric) for u in lines} >= {
        ("hunter", "credits"),
        ("mock_ai", "tokens"),
    }


async def test_export_items_are_immutable(engine, make_settings) -> None:
    await ready_lead(engine, make_settings)
    await exporter(engine, make_settings, InMemorySheetsWriter()).run()
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as conn:
        conn.execute(text("update export_items set action = 'skip'"))


async def test_review_actions_append_decisions_and_audit(engine, make_settings) -> None:
    cid = await qualified_drivn(engine, make_settings)
    q = SqlQualificationStore(engine)
    store = SqlOutputStore(engine)
    original = q.latest_decision(cid)
    assert original is not None and original.decision is QualificationDecision.QUALIFIED
    review = ReviewService(q, store, make_settings())
    reopened = await review.act(cid, ReviewAction.REOPEN, reviewer="Asha", reason="recheck")
    approved = await review.act(
        cid,
        ReviewAction.APPROVE,
        reviewer="Ravi",
        reason="ok after call",
        expected_decision_id=reopened.audit.resulting_decision_id,
    )
    assert reopened.applied and approved.applied
    history = q.history(cid)
    assert [d.decision.value for d in history] == ["qualified", "review", "qualified"]
    assert history[-1] == original  # the first decision is unchanged
    audits = store.human_reviews(cid)
    assert [(a.action, a.reviewer) for a in audits] == [
        (ReviewAction.APPROVE, "Ravi"), (ReviewAction.REOPEN, "Asha"),
    ]  # fmt: skip
    assert audits[0].resulting_decision_id == history[0].id
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as conn:
        conn.execute(text("update human_reviews set reviewer = 'someone else'"))
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as conn:
        conn.execute(text("update qualification_decisions set decision = 'not_qualified'"))


async def test_database_enforces_review_audit_rules(engine, make_settings) -> None:
    cid = await qualified_drivn(engine, make_settings)
    insert = text(
        "insert into human_reviews (id, company_id, action, outcome, reviewer, reason, "
        "policy_version, reviewed_at, refusal_code) "
        "values (:i, :c, 'approve', :o, :r, :why, 'p', now(), :code)"
    )
    bad = [
        ({"o": "applied", "r": "Asha", "why": "fine", "code": None}, "applied_requires_decision"),
        ({"o": "refused", "r": "Asha", "why": "fine", "code": None}, "refused_requires_code"),
        ({"o": "refused", "r": "  ", "why": "fine", "code": "x"}, "reviewer_and_reason_required"),
    ]
    for params, constraint in bad:
        with pytest.raises(IntegrityError, match=constraint), engine.begin() as conn:
            conn.execute(insert, {"i": uuid.uuid4(), "c": cid, **params})
    with engine.begin() as conn:
        conn.execute(
            insert,
            {
                "i": uuid.uuid4(),
                "c": cid,
                "o": "refused",
                "r": "Asha",
                "why": "not in review",
                "code": "not_allowed_from_qualified",
            },
        )
    [audit] = SqlOutputStore(engine).human_reviews(cid)
    assert audit.outcome is ReviewOutcome.REFUSED
