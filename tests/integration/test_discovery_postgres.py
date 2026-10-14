"""Real PostgreSQL tests: migrations, dedupe constraints, provenance, concurrency.

Uses a SEPARATE database (``lead_intel_test``, created automatically) so the real candidate pool
is never touched. The URL comes from ``TEST_DATABASE_URL`` or is derived from ``DATABASE_URL``
in ``.env`` by swapping the database name. Tests are skipped when PostgreSQL is unreachable.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from pydantic import HttpUrl
from sqlalchemy import Engine, create_engine, insert, inspect, make_url, text
from sqlalchemy.exc import IntegrityError, OperationalError

from app.core.config import Settings
from app.db.base import Base
from app.db.models import CompanyRow
from app.db.session import check_schema, expected_schema_revision
from app.domain.enums import ProviderName, RunStatus, SourceType
from app.domain.identity import company_dedupe_key
from app.domain.models import DiscoveryRun
from app.providers.contracts import CandidateRecord, DiscoveryQuery
from app.repositories.discovery import SqlDiscoveryStore
from app.services.discovery import DiscoveryService
from tests.unit.test_discovery_service import ScriptedProvider, cand, result

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
TEST_DB = "lead_intel_test"
TABLES = ("discovery_hits", "sources", "companies", "discovery_runs")


def _test_url() -> str | None:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit
    env_file = ROOT / ".env"
    if not env_file.exists():
        return None
    base = Settings(_env_file=env_file).database_url
    if base is None:
        return None
    return (
        make_url(base.get_secret_value())
        .set(database=TEST_DB)
        .render_as_string(hide_password=False)
    )


def _alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = url
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture(scope="module")
def pg_url() -> str:
    url = _test_url()
    if url is None:
        pytest.skip("no DATABASE_URL/TEST_DATABASE_URL configured")
    # Short connect timeout so a stopped container makes these tests SKIP instead of hanging
    # (seen at Phase 4 start when the lead-intel-pg container had exited).
    admin = create_engine(
        make_url(url).set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=True,
        connect_args={"connect_timeout": 3},
    )
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": TEST_DB}
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
    except OperationalError:
        pytest.skip("PostgreSQL not reachable")
    finally:
        admin.dispose()
    command.upgrade(_alembic_config(url), "head")
    return url


@pytest.fixture
def engine(pg_url: str) -> Iterator[Engine]:
    eng = create_engine(pg_url)
    with eng.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)} CASCADE"))
    yield eng
    eng.dispose()


@pytest.fixture
def store(engine: Engine) -> SqlDiscoveryStore:
    return SqlDiscoveryStore(engine)


def _new_run(store: SqlDiscoveryStore) -> DiscoveryRun:
    run = DiscoveryRun(provider=ProviderName.APIFY, status=RunStatus.RUNNING)
    store.create_run(run)
    return run


# ---------------------------------------------------------------- migrations
def test_migrations_are_reproducible_and_match_models(pg_url: str) -> None:
    cfg = _alembic_config(pg_url)
    command.downgrade(cfg, "base")
    eng = create_engine(pg_url)
    try:
        assert not set(TABLES) & set(inspect(eng).get_table_names())
        command.upgrade(cfg, "head")
        assert set(TABLES) <= set(inspect(eng).get_table_names())
        with eng.connect() as conn:  # ORM models and migration agree: no autogenerate drift
            diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
        assert diff == []
    finally:
        eng.dispose()


def test_schema_readiness_check(pg_url: str, make_settings) -> None:
    s = make_settings(database_url=pg_url)
    state, detail = check_schema(s)
    assert state == "ok"
    head = expected_schema_revision()
    assert head is not None
    assert detail == f"revision {head}"


# ---------------------------------------------------------------- runs
def test_run_round_trip(store: SqlDiscoveryStore) -> None:
    run = _new_run(store)
    run.provider_run_id = "apify-run-1"
    run.provider_input = {"queries": "q", "maximumLeadsEnrichmentRecords": 0}
    run.cost_usd = Decimal("0.0055")
    run.max_charge_usd = Decimal("0.02")
    run.charged_events = {"search-page-scraped": 1}
    run.status = RunStatus.SUCCEEDED
    store.save_run(run)
    loaded = store.get_run(run.id)
    assert loaded.provider_run_id == "apify-run-1"
    assert loaded.cost_usd == Decimal("0.0055")
    assert loaded.provider_input["maximumLeadsEnrichmentRecords"] == 0
    assert loaded.charged_events == {"search-page-scraped": 1}
    assert loaded.status is RunStatus.SUCCEEDED


# ---------------------------------------------------------------- dedupe + provenance
def test_company_and_source_dedupe_with_provenance(store: SqlDiscoveryStore) -> None:
    run1 = _new_run(store)
    out1 = store.persist_candidates(
        run1.id, ProviderName.APIFY, "r1", [cand("Acme Robotics", "https://news.example/a")]
    )
    assert [(o.new_company, o.new_source, o.new_hit) for o in out1] == [(True, True, True)]

    run2 = _new_run(store)
    out2 = store.persist_candidates(
        run2.id,
        ProviderName.APIFY,
        "r2",
        [
            cand("ACME Robotics Pvt. Ltd.", "https://other.example/x"),  # same company
            cand("Acme Robotics", "https://NEWS.example/a/?utm_source=g"),  # same source URL
            cand("Acme Robotics", "https://news.example/a"),  # repeat inside the batch
        ],
    )
    assert [o.new_company for o in out2] == [False, False, False]
    assert [o.new_source for o in out2] == [True, False, False]
    assert [o.new_hit for o in out2] == [True, True, False]  # (run, company, source) unique

    assert store.count_companies() == 1
    company = store.find_company_by_key(company_dedupe_key("Acme Robotics"))
    assert company is not None
    assert company.name == "Acme Robotics"  # first-seen name is kept
    assert len(company.source_ids) == 2
    assert store.get_company(company.id).source_ids == company.source_ids


def test_unique_constraints_are_enforced_by_the_database(engine: Engine) -> None:
    import uuid

    with engine.begin() as conn:
        conn.execute(
            insert(CompanyRow).values(
                id=uuid.uuid4(), dedupe_key="name:x", name="X", normalized_name="x"
            )
        )
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            insert(CompanyRow).values(
                id=uuid.uuid4(), dedupe_key="name:x", name="X2", normalized_name="x"
            )
        )


def test_concurrent_persists_do_not_duplicate(store: SqlDiscoveryStore) -> None:
    runs = [_new_run(store) for _ in range(6)]

    def persist(i: int) -> None:
        store.persist_candidates(
            runs[i].id,
            ProviderName.APIFY,
            f"r{i}",
            [cand("Zeta Labs", f"https://news.example/zeta-{i % 2}")],
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(persist, range(6)))
    assert store.count_companies() == 1
    company = store.find_company_by_key(company_dedupe_key("Zeta Labs"))
    assert company is not None and len(company.source_ids) == 2


def test_domain_is_filled_in_when_later_seen(store: SqlDiscoveryStore) -> None:
    run = _new_run(store)
    store.persist_candidates(
        run.id,
        ProviderName.APIFY,
        None,
        [
            CandidateRecord(
                company_name="Acme",
                domain="acme.io",
                source_url=HttpUrl("https://n.example/1"),
                source_type=SourceType.NEWS,
            )
        ],
    )
    company = store.find_company_by_key("domain:acme.io")
    assert company is not None and company.domain == "acme.io"


# ---------------------------------------------------------------- service end-to-end
async def test_service_end_to_end_on_postgres(store: SqlDiscoveryStore) -> None:
    provider = ScriptedProvider(
        result(cand("Acme Robotics", "https://news.example/a"), run_ref="apify-A"),
        result(cand("acme robotics", "https://news.example/b"), run_ref="apify-B"),
    )
    service = DiscoveryService(provider, store)
    first = await service.run(DiscoveryQuery(source="funding_news"))
    second = await service.run(DiscoveryQuery(source="funding_news"))
    assert store.get_run(first.id).new_company_count == 1
    stored = store.get_run(second.id)
    assert (stored.status, stored.new_company_count, stored.duplicate_count) == (
        RunStatus.SUCCEEDED,
        0,
        1,
    )
    assert stored.provider_run_id == "apify-B"
    assert store.count_companies() == 1


def test_duplicate_provider_run_id_rejected(store: SqlDiscoveryStore) -> None:
    a = _new_run(store)
    a.provider_run_id = "same-apify-run"
    store.save_run(a)
    b = _new_run(store)
    b.provider_run_id = "same-apify-run"
    with pytest.raises(IntegrityError):
        store.save_run(b)
