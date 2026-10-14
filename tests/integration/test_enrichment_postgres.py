"""Phase 6 persistence on real PostgreSQL (separate lead_intel_test database)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.domain.enrichment.records import CacheEntry
from app.domain.enums import (
    ContactStatus,
    CrawlStatus,
    EmailVerificationStatus,
    ProviderName,
    SignalType,
)
from app.domain.models import WebsiteCrawlRecord, utcnow
from app.providers.contracts import WebPage
from app.providers.enrichment_kit.mock import MockEnrichmentKitProvider
from app.providers.hunter.mock import MockHunterProvider
from app.providers.laya import MockLayaProvider
from app.repositories.enrichment import SqlEnrichmentStore
from app.repositories.qualification import SqlQualificationStore
from app.repositories.website import SqlWebsiteStore
from app.services.enrichment import EnrichmentService
from app.services.qualification import QualificationService
from tests.integration.test_discovery_postgres import pg_url  # noqa: F401 (fixture)
from tests.integration.test_qualification_postgres import TABLES as QUAL_TABLES
from tests.integration.test_qualification_postgres import add_signals
from tests.integration.test_signal_research_postgres import make_verified

pytestmark = pytest.mark.postgres

TABLES = (
    "enrichment_cache",
    "enrichment_attempts",
    "enrichment_contacts",
    "enrichment_runs",
    *QUAL_TABLES,
)
TEAM = "## Team\n### Manav Bansal-CEO\nManav Bansal is the CEO and Co-Founder of Drivn."


@pytest.fixture
def engine(pg_url: str) -> Iterator[Engine]:  # noqa: F811
    eng = create_engine(pg_url)
    with eng.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)} CASCADE"))
    yield eng
    eng.dispose()


def add_team_page(engine: Engine, cid: uuid.UUID) -> None:
    from pydantic import HttpUrl

    SqlWebsiteStore(engine).record_crawl(
        WebsiteCrawlRecord(
            company_id=cid, domain="drivn.co.in", provider=ProviderName.CRAWL4AI,
            status=CrawlStatus.SUCCEEDED, page_budget=2, identity_verified=True,
        ),
        [WebPage(url=HttpUrl("https://www.drivn.co.in/team"), markdown=TEAM, title="Team")],
    )  # fmt: skip


async def qualified_drivn(engine: Engine, make_settings) -> uuid.UUID:
    cid = make_verified(engine)
    add_signals(engine, cid, [("Drivn raises Rs 4 crore seed round", SignalType.FUNDING, 10)])
    add_team_page(engine, cid)
    await QualificationService(
        SqlQualificationStore(engine), MockLayaProvider(), make_settings()
    ).run([cid])
    return cid


def service(engine: Engine, make_settings, **kw) -> EnrichmentService:
    return EnrichmentService(
        SqlEnrichmentStore(engine), SqlQualificationStore(engine), MockEnrichmentKitProvider(),
        MockHunterProvider(), make_settings(), **kw,
    )  # fmt: skip


async def test_end_to_end_enrichment_is_persisted_with_provenance(engine, make_settings) -> None:
    cid = await qualified_drivn(engine, make_settings)
    store = SqlEnrichmentStore(engine)
    assert store.qualified_company_ids(10) == [cid]
    run, [c] = await service(engine, make_settings).run()
    assert c.status is ContactStatus.VERIFIED
    assert (c.full_name, c.email) == ("Manav Bansal", "manav.bansal@drivn.co.in")
    with engine.connect() as conn:
        row = conn.execute(text("select * from enrichment_contacts")).mappings().one()
        attempts = conn.execute(
            text(
                "select provider, operation, status, credits from enrichment_attempts"
                " order by created_at"
            )
        ).all()
        run_row = conn.execute(text("select * from enrichment_runs")).mappings().one()
        cache = conn.execute(text("select * from enrichment_cache")).mappings().one()
    assert row["qualification_id"] == c.qualification_id
    assert row["provenance"]["identity"]["sources"] == ["https://www.drivn.co.in/team"]
    assert [tuple(a[:3]) for a in attempts] == [
        ("hunter", "email_finder", "hit"), ("hunter", "email_verifier", "hit"),
    ]  # fmt: skip
    assert run_row["hunter_credits"] == Decimal(2) and run_row["mode"] == "mock"
    assert cache["email"] == "manav.bansal@drivn.co.in" and cache["email_verification"] == "valid"
    assert store.contacts(cid)[0].id == c.id
    assert store.month_credits(utcnow() - timedelta(days=1)) == {ProviderName.HUNTER: Decimal(2)}
    assert [a.operation.value for a in store.attempts(run.id)] == ["email_finder", "email_verifier"]


async def test_second_run_is_served_from_cache(engine, make_settings) -> None:
    await qualified_drivn(engine, make_settings)
    await service(engine, make_settings).run()
    run2, [c2] = await service(engine, make_settings).run()
    assert c2.from_cache and run2.hunter_credits == 0
    with engine.connect() as conn:
        assert conn.execute(text("select hits from enrichment_cache")).scalar() == 1
        assert conn.execute(text("select count(*) from enrichment_contacts")).scalar() == 2


async def test_review_company_is_blocked_and_not_a_candidate(engine, make_settings) -> None:
    cid = make_verified(engine)
    add_signals(engine, cid, [("Drivn raises seed funding", SignalType.FUNDING, 5)])  # REVIEW
    await QualificationService(
        SqlQualificationStore(engine), MockLayaProvider(), make_settings()
    ).run([cid])
    assert SqlEnrichmentStore(engine).qualified_company_ids(10) == []
    kit = MockEnrichmentKitProvider()
    svc = EnrichmentService(
        SqlEnrichmentStore(engine),
        SqlQualificationStore(engine),
        kit,
        MockHunterProvider(),
        make_settings(),
    )
    _, [c] = await svc.run([cid])
    assert c.status is ContactStatus.BLOCKED and kit.calls == []


async def test_history_tables_are_append_only(engine, make_settings) -> None:
    await qualified_drivn(engine, make_settings)
    await service(engine, make_settings).run()
    for table in ("enrichment_contacts", "enrichment_attempts"):
        with pytest.raises(DBAPIError, match="append-only"), engine.begin() as conn:
            conn.execute(text(f"update {table} set run_id = run_id"))  # noqa: S608 - fixed names
    with engine.begin() as conn:  # the cache is mutable by design
        conn.execute(text("update enrichment_cache set hits = hits + 5"))


async def test_database_constraints(engine, make_settings) -> None:
    cid = await qualified_drivn(engine, make_settings)
    run, [done] = await service(engine, make_settings).run()
    base = (
        "insert into enrichment_contacts (id, run_id, company_id, email_verification, status,"
        " policy_version, email, qualification_id) values (:i, :r, :c, :v, :s, 'x', :e, :q)"
    )
    with (
        pytest.raises(IntegrityError, match="verified_requires_valid_email"),
        engine.begin() as conn,
    ):
        conn.execute(
            text(base),
            {
                "i": uuid.uuid4(),
                "r": run.id,
                "c": cid,
                "v": "accept_all",
                "s": "verified",
                "e": "a@drivn.co.in",
                "q": done.qualification_id,
            },
        )
    with (
        pytest.raises(IntegrityError, match="enriched_requires_qualification"),
        engine.begin() as conn,
    ):
        conn.execute(
            text(base),
            {
                "i": uuid.uuid4(),
                "r": run.id,
                "c": cid,
                "v": "unverified",
                "s": "not_found",
                "e": None,
                "q": None,
            },
        )


async def test_cache_upsert_keeps_hits(engine, make_settings) -> None:
    cid = await qualified_drivn(engine, make_settings)
    store = SqlEnrichmentStore(engine)
    now = utcnow()
    entry = CacheEntry(
        cache_key="k1", company_id=cid, person_key="drivn.co.in|x y", domain="drivn.co.in",
        email="x@drivn.co.in", email_verification=EmailVerificationStatus.VALID, verified_at=now,
        confidence=0.9, provider=ProviderName.HUNTER, expires_at=now + timedelta(days=30),
    )  # fmt: skip
    store.cache_put(entry)
    store.cache_hit("k1")
    store.cache_put(
        entry.model_copy(update={"email_verification": EmailVerificationStatus.UNKNOWN})
    )
    got = store.cache_get("k1")
    assert got is not None and got.email_verification is EmailVerificationStatus.UNKNOWN
    with engine.connect() as conn:
        assert conn.execute(text("select hits from enrichment_cache")).scalar() == 1
