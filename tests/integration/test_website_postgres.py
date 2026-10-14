"""Phase 3 persistence on real PostgreSQL (separate lead_intel_test database)."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID

import pytest
from pydantic import HttpUrl
from sqlalchemy import Engine, create_engine, func, insert, select, text
from sqlalchemy.exc import IntegrityError

from app.db.models import (
    CompanyMergeRow,
    CompanyRow,
    DiscoveryHitRow,
    DomainResolutionRow,
    WebsiteCrawlRow,
    WebsitePageRow,
)
from app.domain.enums import CrawlStatus, DomainResolutionStatus, PageType, ProviderName, RunStatus
from app.domain.identity import company_dedupe_key
from app.domain.models import DiscoveryRun, DomainResolutionRecord, WebsiteCrawlRecord
from app.providers.contracts import WebPage
from app.repositories.discovery import SqlDiscoveryStore
from app.repositories.website import SqlWebsiteStore
from tests.integration.test_discovery_postgres import TABLES as DISCOVERY_TABLES
from tests.integration.test_discovery_postgres import pg_url  # noqa: F401 (fixture)
from tests.unit.test_discovery_service import cand

pytestmark = pytest.mark.postgres

TABLES = (
    "company_merges",
    "website_pages",
    "website_crawls",
    "domain_resolutions",
    *DISCOVERY_TABLES,
)


@pytest.fixture
def engine(pg_url: str) -> Iterator[Engine]:  # noqa: F811
    eng = create_engine(pg_url)
    with eng.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)} CASCADE"))
    yield eng
    eng.dispose()


@pytest.fixture
def discovery(engine: Engine) -> SqlDiscoveryStore:
    return SqlDiscoveryStore(engine)


@pytest.fixture
def website(engine: Engine) -> SqlWebsiteStore:
    return SqlWebsiteStore(engine)


def discover(store: SqlDiscoveryStore, name: str, url: str) -> UUID:
    run = DiscoveryRun(provider=ProviderName.APIFY, status=RunStatus.RUNNING)
    store.create_run(run)
    (outcome,) = store.persist_candidates(run.id, ProviderName.APIFY, None, [cand(name, url)])
    return outcome.company_id


def page(url: str, page_type: PageType = PageType.HOME, text_: str = "Drivn content") -> WebPage:
    return WebPage(url=HttpUrl(url), page_type=page_type, markdown=text_, title="Drivn")


def crawl_record(company_id: UUID, pages: int = 1) -> WebsiteCrawlRecord:
    return WebsiteCrawlRecord(
        company_id=company_id,
        domain="drivn.in",
        provider=ProviderName.FIRECRAWL,
        status=CrawlStatus.SUCCEEDED,
        sufficient=True,
        page_budget=8,
        pages_attempted=pages,
        pages_succeeded=pages,
        credits_used=1 + pages,
        facts={"home_title": "Drivn"},
        social_links={"linkedin.com": "https://www.linkedin.com/company/drivn"},
        identity_verified=True,
    )


def count(engine: Engine, table: type, **where: object) -> int:
    stmt = select(func.count()).select_from(table)
    for col, value in where.items():
        stmt = stmt.where(getattr(table, col) == value)
    with engine.connect() as conn:
        return int(conn.execute(stmt).scalar_one())


# ---------------------------------------------------------------- schema
def test_new_company_columns_have_safe_defaults(discovery, website, engine) -> None:
    cid = discover(discovery, "Drivn", "https://news.example/a")
    ctx = website.company_context(cid)
    assert ctx.domain_status is DomainResolutionStatus.UNRESOLVED
    assert ctx.publisher_hosts == frozenset({"news.example"})
    assert website.companies_needing_resolution(10) == [cid]
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text("UPDATE companies SET domain_status = 'bogus'"))


def test_verified_domain_is_unique(engine) -> None:
    import uuid

    rows = [
        {
            "id": uuid.uuid4(),
            "dedupe_key": f"name:{n}",
            "name": n,
            "normalized_name": n,
            "domain": "x.io",
        }
        for n in ("a", "b")
    ]
    with engine.begin() as conn:
        conn.execute(insert(CompanyRow).values(rows[0]))
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(insert(CompanyRow).values(rows[1]))


# ---------------------------------------------------------------- records
def test_resolution_crawl_and_pages_persist(discovery, website, engine) -> None:
    cid = discover(discovery, "Drivn", "https://news.example/a")
    website.record_resolution(
        DomainResolutionRecord(
            company_id=cid,
            provider="firecrawl",
            query='"Drivn" official website India',
            status=DomainResolutionStatus.RESOLVED,
            chosen_domain="drivn.in",
            confidence=0.95,
            candidates=[{"domain": "drivn.in", "score": 0.95}],
            credits_used=2,
        )
    )
    website.set_domain_status(cid, DomainResolutionStatus.RESOLVED, 0.95)
    first = crawl_record(cid, 2)
    website.record_crawl(
        first, [page("https://drivn.in/"), page("https://drivn.in/about", PageType.ABOUT)]
    )
    # re-crawl updates the same page rows instead of duplicating them
    second = crawl_record(cid, 1)
    website.record_crawl(second, [page("https://drivn.in/", text_="Drivn updated")])

    assert count(engine, WebsitePageRow, company_id=cid) == 2
    assert count(engine, WebsiteCrawlRow, company_id=cid) == 2
    with engine.connect() as conn:
        home = conn.execute(
            select(WebsitePageRow.markdown, WebsitePageRow.crawl_id).where(
                WebsitePageRow.url == "https://drivn.in/"
            )
        ).one()
        company = conn.execute(select(CompanyRow).where(CompanyRow.id == cid)).one()
        resolution = conn.execute(select(DomainResolutionRow)).one()
    assert home.markdown == "Drivn updated" and home.crawl_id == second.id
    assert company.domain_status == "resolved" and company.website_crawled_at is not None
    assert resolution.candidates == [{"domain": "drivn.in", "score": 0.95}]


def test_assign_verified_domain_without_conflict(discovery, website, engine) -> None:
    cid = discover(discovery, "Drivn", "https://news.example/a")
    outcome = website.assign_verified_domain(cid, "drivn.in", 0.9, "firecrawl_search", {})
    assert outcome.company_id == cid and outcome.merged_from is None
    with engine.connect() as conn:
        row = conn.execute(select(CompanyRow).where(CompanyRow.id == cid)).one()
    assert (row.domain, row.domain_status, row.domain_source) == (
        "drivn.in",
        "verified",
        "firecrawl_search",
    )
    assert row.dedupe_key == "name:drivn"  # first-seen identity key is preserved


# ---------------------------------------------------------------- merge
def test_merge_preserves_all_provenance_and_creates_alias(discovery, website, engine) -> None:
    owner = discover(discovery, "Drivn", "https://news.example/a")
    website.assign_verified_domain(owner, "drivn.in", 0.95, "firecrawl_search", {})
    website.record_crawl(crawl_record(owner), [page("https://drivn.in/")])

    twin = discover(discovery, "Drivn Mobility Pvt Ltd", "https://other.example/b")
    discover(discovery, "Drivn Mobility", "https://third.example/c")  # same twin, 2nd source
    website.record_resolution(
        DomainResolutionRecord(
            company_id=twin, provider="operator", query="-", status=DomainResolutionStatus.RESOLVED
        )
    )
    website.record_crawl(
        crawl_record(twin, 2),
        [page("https://drivn.in/"), page("https://drivn.in/careers", PageType.CAREERS)],
    )
    hits_before = count(engine, DiscoveryHitRow)

    outcome = website.assign_verified_domain(
        twin, "drivn.in", 0.9, "operator_override", {"verified_by": "homepage_mentions_name"}
    )

    assert outcome.company_id == owner and outcome.merged_from == twin
    assert count(engine, CompanyRow, id=twin) == 0
    assert count(engine, DiscoveryHitRow) == hits_before  # no provenance lost
    assert count(engine, DiscoveryHitRow, company_id=owner) == 3
    assert count(engine, WebsitePageRow, company_id=owner) == 2  # duplicate home dropped
    assert count(engine, WebsiteCrawlRow, company_id=owner) == 2
    assert count(engine, DomainResolutionRow, company_id=owner) == 1
    with engine.connect() as conn:
        merge = conn.execute(select(CompanyMergeRow)).one()
        survivor = conn.execute(select(CompanyRow).where(CompanyRow.id == owner)).one()
    assert merge.from_dedupe_key == company_dedupe_key("Drivn Mobility")
    assert merge.reason == "same_verified_domain"
    assert merge.evidence == {"verified_by": "homepage_mentions_name"}
    assert survivor.times_seen == 3

    # later discovery of the merged name lands on the survivor (alias), not a new row
    again = discover(discovery, "Drivn Mobility", "https://fourth.example/d")
    assert again == owner
    assert count(engine, CompanyRow) == 1


def test_later_discovery_with_verified_domain_maps_to_company(discovery, website) -> None:
    cid = discover(discovery, "Drivn", "https://news.example/a")
    website.assign_verified_domain(cid, "drivn.in", 0.9, "firecrawl_search", {})
    run = DiscoveryRun(provider=ProviderName.APIFY, status=RunStatus.RUNNING)
    discovery.create_run(run)
    candidate = cand("Drivn Electric", "https://news.example/z", domain="drivn.in")
    (out,) = discovery.persist_candidates(run.id, ProviderName.APIFY, None, [candidate])
    assert out.company_id == cid and out.new_company is False
    assert discovery.count_companies() == 1
