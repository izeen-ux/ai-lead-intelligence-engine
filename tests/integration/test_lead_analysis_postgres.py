"""Phase 7 persistence on real PostgreSQL (separate lead_intel_test database)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.domain.analysis.records import AnalysisStatus
from app.domain.enums import RunStatus, SignalType
from app.domain.models import utcnow
from app.providers.ai_router import MockAiRouterProvider
from app.repositories.analysis import SqlAnalysisStore
from app.repositories.enrichment import SqlEnrichmentStore
from app.repositories.qualification import SqlQualificationStore
from app.services.lead_analysis import LeadAnalysisService
from tests.integration.test_discovery_postgres import pg_url  # noqa: F401 (fixture)
from tests.integration.test_enrichment_postgres import TABLES as ENRICH_TABLES
from tests.integration.test_enrichment_postgres import qualified_drivn
from tests.integration.test_enrichment_postgres import service as enrichment_service
from tests.integration.test_qualification_postgres import add_signals
from tests.integration.test_signal_research_postgres import make_verified

pytestmark = pytest.mark.postgres

TABLES = ("lead_analyses", "analysis_runs", *ENRICH_TABLES)


@pytest.fixture
def engine(pg_url: str) -> Iterator[Engine]:  # noqa: F811
    eng = create_engine(pg_url)
    with eng.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)} CASCADE"))
    yield eng
    eng.dispose()


def analysis_service(engine: Engine, make_settings) -> LeadAnalysisService:
    settings = make_settings()
    return LeadAnalysisService(
        SqlAnalysisStore(engine), SqlQualificationStore(engine), SqlEnrichmentStore(engine),
        MockAiRouterProvider(settings), settings,
    )  # fmt: skip


async def eligible_drivn(engine: Engine, make_settings) -> uuid.UUID:
    cid = await qualified_drivn(engine, make_settings)
    await enrichment_service(engine, make_settings).run()
    return cid


async def test_eligibility_query_needs_qualified_and_verified_contact(
    engine, make_settings
) -> None:
    store = SqlAnalysisStore(engine)
    cid = await qualified_drivn(engine, make_settings)
    assert store.eligible_company_ids(10) == []  # qualified, but not enriched yet
    await enrichment_service(engine, make_settings).run()
    assert store.eligible_company_ids(10) == [cid]
    other = make_verified(engine)  # verified domain + signals, but never qualified/enriched
    add_signals(engine, other, [("Drivn mentioned in a list", SignalType.OTHER, 5)])
    assert store.eligible_company_ids(10) == [cid]


async def test_analysis_is_persisted_with_usage_and_provenance(engine, make_settings) -> None:
    cid = await eligible_drivn(engine, make_settings)
    _, [record] = await analysis_service(engine, make_settings).run()
    assert record.status is AnalysisStatus.SUCCEEDED
    store = SqlAnalysisStore(engine)
    [stored] = store.analyses(cid)
    assert stored.id == record.id and stored.company_name == record.company_name
    assert stored.recommended_offer_id == record.recommended_offer_id
    assert stored.supporting_evidence_ids == record.supporting_evidence_ids
    assert stored.usage == record.usage and stored.usage["total_tokens"] > 0
    assert stored.analysis == record.analysis and stored.evidence_bundle == record.evidence_bundle
    assert stored.qualification_id == record.qualification_id
    assert stored.contact_id == record.contact_id
    with engine.connect() as conn:
        row = conn.execute(text("select * from analysis_runs")).mappings().one()
    assert row["status"] == RunStatus.SUCCEEDED.value and row["succeeded_count"] == 1
    assert row["total_tokens"] == record.usage["total_tokens"]
    assert store.month_cost(utcnow() - timedelta(days=1)) == Decimal(0)


async def test_analyses_are_immutable_and_append_only(engine, make_settings) -> None:
    cid = await eligible_drivn(engine, make_settings)
    await analysis_service(engine, make_settings).run([cid])
    await analysis_service(engine, make_settings).run([cid])
    assert len(SqlAnalysisStore(engine).analyses(cid)) == 2
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as conn:
        conn.execute(text("update lead_analyses set recommended_offer_id='ai_audit_nbfc'"))


async def test_database_rejects_ungrounded_success(engine, make_settings) -> None:
    cid = await eligible_drivn(engine, make_settings)
    run, _ = await analysis_service(engine, make_settings).run([cid])
    assert run.succeeded_count == 1
    with (
        pytest.raises(IntegrityError, match="succeeded_requires_grounding"),
        engine.begin() as conn,
    ):
        conn.execute(
            text(
                "insert into lead_analyses (id,run_id,company_id,status,supporting_evidence_ids,"
                "evidence_bundle,evidence_signal_ids,catalog_version,usage,attempts,"
                "policy_version,prompt_version,analyzed_at) values (:i,:r,:c,'succeeded',"
                "'[]','{}','[]','v','{}',1,'p','q',now())"
            ),
            {"i": uuid.uuid4(), "r": run.id, "c": cid},
        )
