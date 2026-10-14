"""Phase 5 persistence on real PostgreSQL (separate lead_intel_test database)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest
from pydantic import HttpUrl
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.errors import EnrichmentNotAllowedError
from app.domain.enums import (
    ProviderName,
    QualificationDecision,
    QualificationPath,
    ResearchRunStatus,
    RunStatus,
    SignalType,
)
from app.domain.models import SignalResearchRunRecord, utcnow
from app.domain.qualification.icp import POLICY_VERSION
from app.providers.contracts import EvidenceItem
from app.providers.laya import MockLayaProvider
from app.repositories.discovery import SqlDiscoveryStore
from app.repositories.qualification import SqlQualificationStore
from app.repositories.signals import SqlSignalStore
from app.services.enrichment_gate import ensure_enrichment_allowed
from app.services.qualification import QualificationService
from tests.integration.test_discovery_postgres import pg_url  # noqa: F401 (fixture)
from tests.integration.test_signal_research_postgres import TABLES as SIGNAL_TABLES
from tests.integration.test_signal_research_postgres import make_verified
from tests.integration.test_website_postgres import discover

pytestmark = pytest.mark.postgres

TABLES = ("qualification_decisions", "qualification_runs", *SIGNAL_TABLES)


@pytest.fixture
def engine(pg_url: str) -> Iterator[Engine]:  # noqa: F811
    eng = create_engine(pg_url)
    with eng.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)} CASCADE"))
    yield eng
    eng.dispose()


def add_signals(engine: Engine, cid: uuid.UUID, titles: list[tuple[str, SignalType, int]]) -> None:
    store = SqlSignalStore(engine)
    run = SignalResearchRunRecord(
        company_id=cid,
        provider=ProviderName.AGENT_REACH,
        status=ResearchRunStatus.SUCCEEDED,
        max_queries=5,
        max_items=20,
    )
    store.create_run(run)
    items = [
        EvidenceItem(
            signal_type=t,
            url=HttpUrl(f"https://news.example/{i}"),
            snippet=title,
            title=title,
            platform=f"news:Outlet{i}",
            channel="news_rss",
            published_at=utcnow() - timedelta(days=days),
            confidence=0.7,
            evidence_notes=[
                "news_rss item names the company",
                "matches verified-website context: leasing",
            ],
        )
        for i, (title, t, days) in enumerate(titles)
    ]
    store.persist_signals(run.id, cid, items)


async def run_service(engine: Engine, make_settings, ids: list[uuid.UUID] | None = None):
    service = QualificationService(
        SqlQualificationStore(engine), MockLayaProvider(), make_settings()
    )
    return await service.run(ids, limit=10)


async def test_end_to_end_decisions_are_persisted_with_provenance(engine, make_settings) -> None:
    cid = make_verified(engine)
    titles = [
        (f"Drivn raises Rs 4 crore seed round - outlet {i}", SignalType.FUNDING, 10)
        for i in range(3)
    ]
    add_signals(engine, cid, titles)
    unverified = discover(SqlDiscoveryStore(engine), "Iztri", "https://news.example/iztri")

    run, decisions = await run_service(engine, make_settings)
    assert run.status is RunStatus.SUCCEEDED
    by_company = {d.company_id: d for d in decisions}
    assert by_company[cid].decision is QualificationDecision.QUALIFIED
    assert (by_company[cid].signal_count, by_company[cid].event_count) == (3, 1)
    assert by_company[unverified].decision is QualificationDecision.NOT_QUALIFIED

    store = SqlQualificationStore(engine)
    latest = store.latest_decision(cid)
    assert latest is not None
    assert latest.path is QualificationPath.FUNDING
    assert latest.policy_version == POLICY_VERSION
    assert len(latest.evidence_signal_ids) == 3
    assert latest.evidence_bundle["events"][0]["publishers"] == 3
    with engine.connect() as conn:
        signal_ids = {
            str(r[0])
            for r in conn.execute(text("select id from signals where company_id=:c"), {"c": cid})
        }
        row = conn.execute(text("select * from qualification_runs")).mappings().one()
    assert {str(i) for i in latest.evidence_signal_ids} == signal_ids  # real signal rows
    assert (row["qualified_count"], row["not_qualified_count"]) == (1, 1)
    assert row["icp_config"]["policy_version"] == POLICY_VERSION
    assert ensure_enrichment_allowed(store, cid).id == latest.id
    with pytest.raises(EnrichmentNotAllowedError):
        ensure_enrichment_allowed(store, unverified)


async def test_history_is_immutable_and_append_only(engine, make_settings) -> None:
    cid = make_verified(engine)
    add_signals(engine, cid, [("Drivn raises seed funding", SignalType.FUNDING, 5)])
    await run_service(engine, make_settings, [cid])
    await run_service(engine, make_settings, [cid])
    store = SqlQualificationStore(engine)
    history = store.history(cid)
    assert len(history) == 2
    assert all(d.decision is QualificationDecision.REVIEW for d in history)
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as conn:
        conn.execute(text("update qualification_decisions set decision='qualified'"))
    assert [d.decision for d in store.history(cid)] == [QualificationDecision.REVIEW] * 2


def test_database_rejects_qualified_without_hard_rules(engine) -> None:
    cid = make_verified(engine)
    run_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "insert into qualification_runs (id,status,policy_version,laya_mode,"
                "llm_review_mode,started_at) values (:i,'succeeded','x','mock','disabled',now())"
            ),
            {"i": run_id},
        )
    with (
        pytest.raises(IntegrityError, match="qualified_requires_hard_rules"),
        engine.begin() as conn,
    ):
        conn.execute(
            text(
                "insert into qualification_decisions (id,run_id,company_id,decision,path,"
                "hard_rules_passed,laya_status,llm_review_status,policy_version,decided_at) "
                "values (:i,:r,:c,'qualified','funding',false,'ok','skipped','x',now())"
            ),
            {"i": uuid.uuid4(), "r": run_id, "c": cid},
        )


def test_database_is_at_the_migration_head(engine) -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = __import__("pathlib").Path(__file__).resolve().parents[2]
    head = ScriptDirectory.from_config(Config(str(root / "alembic.ini"))).get_current_head()
    with engine.connect() as conn:
        assert conn.execute(text("select version_num from alembic_version")).scalar() == head
    assert head is not None and head >= "0005"
