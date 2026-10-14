"""Final lead-output expansion on real PostgreSQL: several companies through every stage,
multi-source discovery, the phone columns (migration 0010), the stored-facts profile, an
idempotent re-run and the demo-data guard."""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import IntegrityError

from app.domain.analysis.catalog import default_catalog
from app.domain.enums import ContactStatus, PhoneStatus, QualificationDecision
from app.domain.outputs.leads import LEAD_FIELDS, lead_cells
from app.domain.pipeline import PipelineStatus, Stage, StageStatus, Trigger
from app.providers.registry import ProviderRegistry
from app.repositories.analysis import SqlAnalysisStore
from app.repositories.enrichment import SqlEnrichmentStore
from app.repositories.outputs import SqlOutputStore
from app.repositories.pipeline import SqlPipelineStore
from app.repositories.qualification import SqlQualificationStore
from app.services.leads import LeadViewService
from app.services.pipeline import PipelineService
from app.services.pipeline_stages import core_stages, sql_stages
from tests.integration.test_discovery_postgres import pg_url  # noqa: F401 (fixture)
from tests.integration.test_pipeline_postgres import TABLES, count

pytestmark = pytest.mark.postgres
ROOT = Path(__file__).resolve().parents[2]
STAGES = [Stage.QUALIFICATION, Stage.ENRICHMENT, Stage.ANALYSIS, Stage.EXPORT]


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def engine(pg_url: str) -> Iterator[Engine]:  # noqa: F811
    eng = create_engine(pg_url)
    with eng.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)} CASCADE"))
    yield eng
    eng.dispose()


def pipeline(engine: Engine, settings) -> PipelineService:
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


def lead_rows(engine: Engine) -> dict[str, dict[str, str]]:
    views = LeadViewService(
        SqlQualificationStore(engine), SqlEnrichmentStore(engine), SqlAnalysisStore(engine)
    )
    ids = SqlOutputStore(engine).company_ids_by_decision(QualificationDecision.QUALIFIED, 50)
    out = {}
    for view in views.views(ids):
        cells = lead_cells(view, default_catalog(), LEAD_FIELDS)
        out[view.company_name] = dict(zip(LEAD_FIELDS, cells, strict=True))
    return out


# ================================================================== several companies, every stage
async def test_seeded_companies_flow_through_every_stage_independently(
    engine, make_settings
) -> None:
    seeder = load("seed_demo_db")
    settings = make_settings()
    qrun, decisions, erun = await seeder.seed(engine, settings)
    assert (qrun.qualified_count, qrun.review_count, qrun.not_qualified_count) == (6, 1, 1)
    assert len({d.company_id for d in decisions}) == 8
    assert erun.verified_count == 5 and erun.error_code is None  # one founder is not findable

    run = await pipeline(engine, make_settings(analysis_max_per_run=10)).run(
        trigger=Trigger.CLI, dry_run=False, limit=10, stages=[Stage.ANALYSIS, Stage.EXPORT]
    )
    assert run.status is PipelineStatus.SUCCEEDED and run.company_limit == 10
    assert run.result(Stage.ANALYSIS).counts["succeeded"] == 5  # type: ignore[union-attr]
    assert run.result(Stage.EXPORT).counts["leads"] == 7  # type: ignore[union-attr]
    with engine.connect() as conn:
        analysed = conn.execute(
            text("select count(distinct company_id) from lead_analyses where status = 'succeeded'")
        ).scalar_one()
    assert analysed == 5

    rows = lead_rows(engine)
    assert len(rows) == 6 == len({r["lead_id"] for r in rows.values()})
    ready = [r for r in rows.values() if r["export_status"] == "ready"]
    assert len(ready) == 5 and len({r["founder_email"] for r in ready}) == 5
    kheti = rows["Kheti Labs"]
    assert (kheti["company_location"], kheti["industry"]) == ("India", "Agritech")
    assert kheti["company_linkedin_url"].endswith("/company/synthetic-fixture-kheti-labs")
    assert (kheti["funding_amount"], kheti["funding_round"]) == ("₹4 Cr", "seed")
    assert kheti["founder_phone"].startswith("+91 00000 ") and kheti["phone_status"] == "found"
    assert rows["Freightmind"]["export_status"] == "awaiting_contact"


async def test_second_run_repeats_nothing(engine, make_settings) -> None:
    seeder = load("seed_demo_db")
    await seeder.seed(engine, make_settings())
    runner = pipeline(engine, make_settings(analysis_max_per_run=10))
    await runner.run(trigger=Trigger.CLI, dry_run=False, limit=10, stages=STAGES)
    tables = ("qualification_decisions", "lead_analyses")
    before = {t: count(engine, t) for t in tables}
    first_rows = lead_rows(engine)
    again = await runner.run(trigger=Trigger.CLI, dry_run=False, limit=10, stages=STAGES)
    skipped = {s.stage for s in again.stages if s.status is StageStatus.SKIPPED}
    assert {Stage.QUALIFICATION, Stage.ANALYSIS} <= skipped
    assert {t: count(engine, t) for t in tables} == before
    assert {n: r["lead_id"] for n, r in lead_rows(engine).items()} == {
        n: r["lead_id"] for n, r in first_rows.items()
    }


async def test_non_qualified_companies_never_get_a_contact_or_analysis(
    engine, make_settings
) -> None:
    seeder = load("seed_demo_db")
    _, decisions, _ = await seeder.seed(engine, make_settings())
    await pipeline(engine, make_settings(analysis_max_per_run=10)).run(
        trigger=Trigger.CLI, dry_run=False, limit=10, stages=STAGES
    )
    estore, astore = SqlEnrichmentStore(engine), SqlAnalysisStore(engine)
    blocked = [d for d in decisions if d.decision is not QualificationDecision.QUALIFIED]
    assert {d.company_name for d in blocked} == {"Drivnly", "Tiffinly"}
    for d in blocked:
        assert all(c.status is ContactStatus.BLOCKED for c in estore.contacts(d.company_id))
        assert not [a for a in astore.analyses(d.company_id) if a.status.value == "succeeded"]


# ================================================================== phone columns (0010)
async def test_phone_and_status_round_trip_and_are_constrained(engine, make_settings) -> None:
    seeder = load("seed_demo_db")
    await seeder.seed(engine, make_settings())
    estore = SqlEnrichmentStore(engine)
    with engine.connect() as conn:
        ids = list(conn.execute(text("select distinct company_id from enrichment_contacts")))
    contacts = [c for (cid,) in ids for c in estore.contacts(cid)]
    statuses = {c.phone_status for c in contacts}
    assert {PhoneStatus.FOUND, PhoneStatus.NOT_FOUND, PhoneStatus.NOT_REQUESTED} <= statuses
    assert all((c.phone is not None) == (c.phone_status is PhoneStatus.FOUND) for c in contacts)
    with engine.connect() as conn:
        cached = conn.execute(
            text("select count(*) from enrichment_cache where phone_status = 'found'")
        ).scalar_one()
    assert cached == 4
    with pytest.raises(IntegrityError, match="phone_matches_status"), engine.begin() as conn:
        conn.execute(text("update enrichment_cache set phone = null where phone is not null"))
    with pytest.raises(IntegrityError, match="phone_status_valid"), engine.begin() as conn:
        conn.execute(
            text("update enrichment_cache set phone_status = 'guessed' where phone is null")
        )


# ================================================================== discovery stage
async def test_discovery_stage_runs_every_enabled_source(engine, make_settings) -> None:
    settings = make_settings(
        discovery_sources_enabled="funding_news,startup_news,linkedin",  # linkedin stays blocked
        apify_max_items_per_run=3,
        discovery_queries="q1;q2;q3;q4",
    )
    store = SqlPipelineStore(engine)
    runner = PipelineService(
        store, sql_stages(settings, engine, ProviderRegistry(settings)), settings,
        allow_mock_writes=True,
    )  # fmt: skip
    run = await runner.run(trigger=Trigger.CLI, dry_run=False, limit=10, stages=[Stage.DISCOVERY])
    counts = run.result(Stage.DISCOVERY).counts  # type: ignore[union-attr]
    assert run.status is PipelineStatus.SUCCEEDED
    assert (counts["sources"], counts["queries"], counts["failed"]) == (2, 6, 0)
    assert counts["candidates"] == 6 and counts["new_companies"] == 3  # 2nd source: duplicates
    assert count(engine, "companies") == 3
    with engine.connect() as conn:
        found = conn.execute(text("select query->>'source', query->'keywords' from discovery_runs"))
        queries = {row[0]: row[1] for row in found}
    assert queries["funding_news"] == ["q1", "q2", "q3"] and len(queries["startup_news"]) == 3
    assert "linkedin" not in queries
    assert store.discovery_run_count("funding_news") == 1
    # the next run takes the NEXT block of queries instead of repeating the same search
    await runner.run(trigger=Trigger.CLI, dry_run=False, limit=10, stages=[Stage.DISCOVERY])
    with engine.connect() as conn:
        latest = conn.execute(
            text(
                "select query->'keywords' from discovery_runs where query->>'source' = "
                "'funding_news' order by created_at desc limit 1"
            )
        ).scalar_one()
    assert latest == ["q4", "q1", "q2"]  # 4 queries, 3 per run


async def test_discovery_is_skipped_in_a_dry_run(engine, make_settings) -> None:
    settings = make_settings()
    runner = PipelineService(
        SqlPipelineStore(engine), sql_stages(settings, engine, ProviderRegistry(settings)), settings
    )
    run = await runner.run(trigger=Trigger.CLI, stages=[Stage.DISCOVERY])
    assert run.result(Stage.DISCOVERY).status is StageStatus.SKIPPED  # type: ignore[union-attr]
    assert count(engine, "discovery_runs") == 0


# ================================================================== demo isolation
def test_seeder_refuses_any_database_that_is_not_the_demo_database(monkeypatch, capsys) -> None:
    import asyncio

    seeder = load("seed_demo_db")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:1/lead_intel")
    monkeypatch.delenv("DATABASE_NAME_OVERRIDE", raising=False)
    monkeypatch.setattr(seeder, "reset_demo_schema", lambda s: pytest.fail("must not reset"))
    monkeypatch.setattr(seeder, "seed", lambda *a: pytest.fail("must not seed"))
    assert asyncio.run(seeder.main_async(["--reset"])) == 2
    assert "REFUSING" in capsys.readouterr().out


# ================================================================== lead owners (0012)
async def test_run_owner_gets_the_leads_and_the_first_owner_is_kept(engine, make_settings) -> None:
    seeder = load("seed_demo_db")
    _, decisions, _ = await seeder.seed(engine, make_settings())
    ids = [d.company_id for d in decisions]
    store = SqlPipelineStore(engine)
    runner = PipelineService(store, {}, make_settings())
    first = runner.new_run(trigger=Trigger.API, owner="meera")
    await runner.start(first)
    assert store.claim_unowned(ids[:3], "Meera", first.id) == 3
    assert store.claim_unowned(ids, "Ravi", first.id) == len(ids) - 3  # never re-assigned
    await runner.execute(first)
    estore = SqlEnrichmentStore(engine)
    owners = [estore.load_company(cid).owner for cid in ids]
    assert owners[:3] == ["Meera"] * 3 and set(owners[3:]) == {"Ravi"}
    rows = lead_rows(engine)
    assert {r["lead_owner"] for r in rows.values()} <= {"Meera", "Ravi"}
    assert all(r["lead_owner"] for r in rows.values())


async def test_discovery_assigns_new_companies_to_the_run_owner(engine, make_settings) -> None:
    settings = make_settings()
    store = SqlPipelineStore(engine)
    runner = PipelineService(
        store, sql_stages(settings, engine, ProviderRegistry(settings)), settings,
        allow_mock_writes=True,
    )  # fmt: skip
    run = await runner.run(
        trigger=Trigger.API, dry_run=False, limit=10, stages=[Stage.DISCOVERY],
        owner="Asha", prompt="fintech startup raises seed\nagritech raises seed",
    )  # fmt: skip
    assert run.status is PipelineStatus.SUCCEEDED
    with engine.connect() as conn:
        owned = conn.execute(text("select owner, count(*) from lead_owners group by 1")).all()
        sent = conn.execute(text("select query->'keywords' from discovery_runs")).scalar_one()
    assert [tuple(r) for r in owned] == [("Asha", 3)]
    assert sent == ["fintech startup raises seed"]  # the prompt replaced the saved queries


async def test_a_run_with_discovery_works_on_its_own_new_companies_first(
    engine, make_settings
) -> None:
    """Regression: a prompt run processed the oldest waiting companies, not what it found."""
    from app.repositories.prescreen import SqlPrescreenStore

    with engine.begin() as conn:
        for name, age in (("Oldco", 5), ("Midco", 3), ("Newco", 1)):
            conn.execute(
                text(
                    "insert into companies (id, name, normalized_name, dedupe_key, domain_status, "
                    "first_seen_at, last_seen_at) values (gen_random_uuid(), :n, :k, :k, "
                    "'unresolved', now() - make_interval(days => :d), now())"
                ),
                {"n": name, "k": name.lower(), "d": age},
            )
    store = SqlPrescreenStore(engine)
    assert [f.name for f in store.candidates(2)] == ["Oldco", "Midco"]
    assert [f.name for f in store.candidates(2, newest_first=True)] == ["Newco", "Midco"]
    assert len(store.scrape_candidates(5, required=False, newest_first=True)) == 3
    assert store.scrape_candidates(5, required=True) == []  # nothing has a green signal yet
