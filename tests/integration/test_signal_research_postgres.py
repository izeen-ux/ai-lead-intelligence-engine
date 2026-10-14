"""Phase 4 persistence on real PostgreSQL (separate lead_intel_test database)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from pydantic import HttpUrl
from sqlalchemy import Engine, create_engine, func, select, text, update
from sqlalchemy.exc import IntegrityError

from app.db.models import CompanyRow, SignalResearchRunRow, SignalRow, SourceRow
from app.domain.enums import (
    CrawlStatus,
    DomainResolutionStatus,
    ProviderName,
    ResearchRunStatus,
    SignalType,
)
from app.domain.models import SignalResearchRunRecord, WebsiteCrawlRecord
from app.providers.agent_reach.mock import MockAgentReachProvider
from app.providers.contracts import EvidenceItem
from app.repositories.signals import SqlSignalStore
from app.repositories.website import SqlWebsiteStore
from app.services.signal_research import SignalResearchService
from tests.integration.test_discovery_postgres import pg_url  # noqa: F401 (fixture)
from tests.integration.test_website_postgres import TABLES as WEBSITE_TABLES
from tests.integration.test_website_postgres import discover

pytestmark = pytest.mark.postgres

TABLES = ("signals", "signal_research_runs", *WEBSITE_TABLES)


@pytest.fixture
def engine(pg_url: str) -> Iterator[Engine]:  # noqa: F811
    eng = create_engine(pg_url)
    with eng.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)} CASCADE"))
    yield eng
    eng.dispose()


@pytest.fixture
def store(engine: Engine) -> SqlSignalStore:
    return SqlSignalStore(engine)


def make_verified(engine: Engine, name: str = "Drivn", domain: str = "drivn.co.in") -> UUID:
    from app.repositories.discovery import SqlDiscoveryStore

    cid = discover(SqlDiscoveryStore(engine), name, f"https://news.example/{name.lower()}")
    website = SqlWebsiteStore(engine)
    website.record_crawl(
        WebsiteCrawlRecord(
            company_id=cid,
            domain=domain,
            provider=ProviderName.FIRECRAWL,
            status=CrawlStatus.SUCCEEDED,
            page_budget=3,
            identity_verified=True,
            facts={"home_title": "Drivn Transition | Commercial EV Leasing"},
        ),
        [],
    )
    website.assign_verified_domain(cid, domain, 1.0, "firecrawl_search", {})
    return cid


def evidence(url: str, signal_type: SignalType = SignalType.FUNDING, **kw: Any) -> EvidenceItem:
    base: dict[str, Any] = {
        "signal_type": signal_type,
        "url": HttpUrl(url),
        "snippet": "Drivn raises Rs 45 Cr",
        "title": "Drivn raises Rs 45 Cr",
        "platform": "news:Entrackr",
        "channel": "news_rss",
        "query": '"Drivn"',
        "confidence": 0.7,
        "evidence_notes": ["news_rss item names the company"],
    }
    return EvidenceItem(**(base | kw))


def count(engine: Engine, table: type, **where: object) -> int:
    stmt = select(func.count()).select_from(table)
    for col, value in where.items():
        stmt = stmt.where(getattr(table, col) == value)
    with engine.connect() as conn:
        return int(conn.execute(stmt).scalar_one())


def new_run(store: SqlSignalStore, cid: UUID) -> SignalResearchRunRecord:
    run = SignalResearchRunRecord(
        company_id=cid,
        provider=ProviderName.AGENT_REACH,
        status=ResearchRunStatus.SUCCEEDED,
        max_queries=5,
        max_items=20,
    )
    store.create_run(run)
    return run


# ---------------------------------------------------------------- targets / gate data
def test_only_verified_companies_are_targets(engine, store) -> None:
    cid = make_verified(engine)
    from app.repositories.discovery import SqlDiscoveryStore

    unresolved = discover(SqlDiscoveryStore(engine), "Iztri", "https://news.example/iztri")
    targets = store.verified_targets(10)
    assert [t.company_id for t in targets] == [cid]
    # D4 (Phase 5): the company name is not a context term any more
    assert targets[0].context_terms[:2] == ("transition", "commercial")
    assert store.research_target(unresolved).is_verified is False


# ---------------------------------------------------------------- persistence
def test_signals_persist_with_provenance_and_dedupe(engine, store) -> None:
    cid = make_verified(engine)
    run = new_run(store, cid)
    items = [
        evidence("https://news.google.com/rss/articles/A"),
        evidence("https://news.google.com/rss/articles/A", SignalType.EXPANSION),  # same source
        evidence(
            "https://www.youtube.com/watch?v=1",
            SignalType.PRODUCT_CUSTOMER,
            channel="youtube",
            platform="youtube:Founder Thesis",
        ),
    ]
    first = store.persist_signals(run.id, cid, items)
    again = store.persist_signals(run.id, cid, items[:1])

    assert (first.created, first.duplicate, again.created, again.duplicate) == (3, 0, 0, 1)
    assert count(engine, SignalRow, company_id=cid) == 3
    assert count(engine, SourceRow, provider="agent_reach") == 2  # sources deduped by URL
    with engine.connect() as conn:
        row = conn.execute(select(SignalRow).where(SignalRow.signal_type == "funding")).one()
        yt_source = conn.execute(select(SourceRow).where(SourceRow.url.like("%youtube%"))).one()
        company = conn.execute(select(CompanyRow).where(CompanyRow.id == cid)).one()
    assert (row.channel, row.platform, row.query) == ("news_rss", "news:Entrackr", '"Drivn"')
    assert row.evidence == "Drivn raises Rs 45 Cr" and float(row.confidence) == 0.7
    assert row.evidence_notes == ["news_rss item names the company"]
    assert row.run_id == run.id
    assert yt_source.source_type == "social"
    assert company.signals_researched_at is not None
    assert company.domain == "drivn.co.in"  # research never changes identity
    assert {s.signal_type for s in store.list_signals(cid)} == {
        SignalType.FUNDING,
        SignalType.EXPANSION,
        SignalType.PRODUCT_CUSTOMER,
    }


def test_confidence_bounds_enforced_by_database(engine, store) -> None:
    cid = make_verified(engine)
    store.persist_signals(new_run(store, cid).id, cid, [evidence("https://n.example/x")])
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(update(SignalRow).values(confidence=1.5))


def test_run_status_check_and_round_trip(engine, store) -> None:
    cid = make_verified(engine)
    run = new_run(store, cid)
    run.queries = [{"channel": "news_rss", "query": '"Drivn"', "status": "ok"}]
    run.queries_used, run.items_processed, run.signals_created = 1, 7, 3
    store.save_run(run)
    with engine.connect() as conn:
        row = conn.execute(select(SignalResearchRunRow)).one()
    assert (row.queries_used, row.items_processed, row.signals_created) == (1, 7, 3)
    assert row.queries[0]["channel"] == "news_rss"
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(update(SignalResearchRunRow).values(status="running"))


# ---------------------------------------------------------------- service end-to-end
async def test_service_end_to_end_on_postgres(engine, store, settings) -> None:
    cid = make_verified(engine)
    from app.repositories.discovery import SqlDiscoveryStore

    unresolved = discover(SqlDiscoveryStore(engine), "Iztri", "https://news.example/iztri")
    service = SignalResearchService(MockAgentReachProvider(), store, settings)
    ok = await service.run(cid)
    skipped = await service.run(unresolved)
    assert (ok.status, ok.signals_created) == (ResearchRunStatus.SUCCEEDED, 3)
    assert skipped.status is ResearchRunStatus.SKIPPED
    assert count(engine, SignalRow, company_id=unresolved) == 0
    assert count(engine, SignalResearchRunRow) == 2
    with engine.connect() as conn:
        status = conn.execute(
            select(CompanyRow.domain_status).where(CompanyRow.id == unresolved)
        ).scalar_one()
    assert status == DomainResolutionStatus.UNRESOLVED.value  # gate never mutates identity
