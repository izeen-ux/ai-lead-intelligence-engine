"""Seed the DEMO database with clearly synthetic companies (Phase 10 acceptance / manager demo).

It refuses to run unless the database in use is named ``lead_intel_demo...`` - select it with
DATABASE_NAME_OVERRIDE=lead_intel_demo for this process. The live database is never touched.

What it writes (all synthetic, from the labelled evaluation set + demo pages; at least five
distinct companies that pass the normal gates, one REVIEW and one NOT_QUALIFIED):
  discovery hit -> verified domain + own-site page -> news signals
then it runs the REAL Phase 5 qualification service (mock Laya) and the REAL Phase 6 enrichment
service (mock providers) so decisions and contacts are produced by the normal rules and gates -
nothing is forged. Contacts carry the mock providers' synthetic markers (``+91 00000 ...``
phones, ``synthetic-fixture-`` LinkedIn slugs). AI analysis is NOT run here: do that with the
pipeline, e.g.

  $env:DATABASE_NAME_OVERRIDE="lead_intel_demo"
  uv run python scripts/seed_demo_db.py            # add --reset to rebuild an older demo DB
  uv run python scripts/run_pipeline.py --db                      # dry run on the demo data

--reset drops and re-creates the schema of the DEMO database only (same name guard), migrates
it to head and seeds it again.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta

from pydantic import HttpUrl
from sqlalchemy import Engine, text

from app.core.config import ProviderMode, Settings
from app.core.logging import configure_logging
from app.db.session import check_schema, get_engine
from app.demo import DEMO_PAGES, demo_context, demo_settings
from app.domain.enrichment.records import EnrichmentRunRecord
from app.domain.enums import (
    CrawlStatus,
    PageType,
    ProviderName,
    ResearchRunStatus,
    RunStatus,
    SourceType,
)
from app.domain.models import DiscoveryRun, SignalResearchRunRecord, WebsiteCrawlRecord, utcnow
from app.domain.qualification.records import (
    QualificationDecisionRecord,
    QualificationRunRecord,
)
from app.evaluation.qualification import load_dataset
from app.providers.contracts import CandidateRecord, EvidenceItem, WebPage
from app.providers.registry import ProviderRegistry
from app.repositories.discovery import SqlDiscoveryStore
from app.repositories.enrichment import SqlEnrichmentStore
from app.repositories.qualification import SqlQualificationStore
from app.repositories.signals import SqlSignalStore
from app.repositories.website import SqlWebsiteStore
from app.services.enrichment import EnrichmentService
from app.services.qualification import QualificationService

DEMO_DB_PREFIX = "lead_intel_demo"


def reset_demo_schema(settings: Settings) -> None:
    """Empty the DEMO database (caller has already checked the name) and migrate to head."""
    from alembic import command
    from alembic.config import Config

    from app.db.session import dispose_engine

    with get_engine(settings).begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    dispose_engine()
    command.upgrade(Config("alembic.ini"), "head")


async def seed(
    engine: Engine, settings: Settings
) -> tuple[QualificationRunRecord, list[QualificationDecisionRecord], EnrichmentRunRecord]:
    """Write the synthetic companies and run the REAL qualification + enrichment services
    (mock providers) over them. The caller is responsible for the database-name guard."""
    discovery, website, signals = (
        SqlDiscoveryStore(engine), SqlWebsiteStore(engine), SqlSignalStore(engine),
    )  # fmt: skip
    dataset = load_dataset()
    now = utcnow()
    ids = []
    for case in dataset.cases:
        if case.id not in DEMO_PAGES or case.company.domain is None:
            continue
        run = DiscoveryRun(provider=ProviderName.APIFY, status=RunStatus.RUNNING)
        discovery.create_run(run)
        candidate = CandidateRecord(
            company_name=case.company.name,
            source_url=HttpUrl(f"https://news.example/{case.id.lower()}/discovery"),
            source_type=SourceType.NEWS,
            source_title=f"SYNTHETIC demo source for {case.company.name}",
            raw={"synthetic": True, "case": case.id, "discovery_source": "synthetic_demo"},
        )
        (outcome,) = discovery.persist_candidates(run.id, ProviderName.APIFY, None, [candidate])
        cid = outcome.company_id
        domain = case.company.domain
        context = demo_context(case.id, case.company.name, DEMO_PAGES[case.id])
        website.record_crawl(
            WebsiteCrawlRecord(
                company_id=cid, domain=domain, provider=ProviderName.FIRECRAWL,
                status=CrawlStatus.SUCCEEDED, page_budget=1, pages_attempted=1,
                pages_succeeded=1, identity_verified=True,
                facts={"home_title": case.company.name, "synthetic": True,
                       "home_description": context["description"] or ""},
                social_links=context["social_links"],  # type: ignore[arg-type]
            ),
            [WebPage(url=HttpUrl(f"https://{domain}/about"), page_type=PageType.ABOUT,
                     markdown=DEMO_PAGES[case.id], title=f"About {case.company.name}")],
        )  # fmt: skip
        website.assign_verified_domain(cid, domain, 1.0, "synthetic_demo", {"case": case.id})
        research = SignalResearchRunRecord(
            company_id=cid, provider=ProviderName.AGENT_REACH,
            status=ResearchRunStatus.SUCCEEDED, max_queries=1, max_items=len(case.signals) or 1,
        )  # fmt: skip
        signals.create_run(research)
        items = []
        for s in case.signals:
            notes = ["news_rss item names the company"]
            if s.attributed:
                notes.append("matches verified-website context: synthetic")
            items.append(
                EvidenceItem(
                    signal_type=s.type,
                    url=HttpUrl(f"https://news.example/{case.id.lower()}/{s.source}"),
                    snippet=s.evidence or s.title,
                    title=s.title,
                    platform=s.platform,
                    channel="news_rss",
                    published_at=now - timedelta(days=s.days_ago)
                    if s.days_ago is not None
                    else None,
                    confidence=s.confidence,
                    evidence_notes=notes,
                )
            )
        signals.persist_signals(research.id, cid, items)
        ids.append(cid)

    # synthetic data + free mock providers: demo count budgets, phone capability visible
    mock = demo_settings(settings).model_copy(update={"provider_mode": ProviderMode.MOCK})
    registry = ProviderRegistry(mock)  # decisions/contacts come from the real services + mocks
    qstore, estore = SqlQualificationStore(engine), SqlEnrichmentStore(engine)
    qrun, decisions = await QualificationService(qstore, registry.qualification, mock).run(ids)
    erun, _ = await EnrichmentService(
        estore, qstore, registry.enrichment, registry.email_verifier, mock
    ).run(ids, limit=len(ids))
    return qrun, decisions, erun


async def main_async(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the synthetic demo database.")
    parser.add_argument("--reset", action="store_true", help="rebuild the DEMO database first")
    args = parser.parse_args(argv)
    settings = Settings()
    configure_logging(level="WARNING", fmt="console", secret_values=settings.secret_values())
    name = settings.database_name or ""
    if not name.startswith(DEMO_DB_PREFIX):
        print(
            f"REFUSING: this seeds SYNTHETIC data and only runs on a database named "
            f"'{DEMO_DB_PREFIX}...' (current: {name or 'not configured'}).\n"
            f'  Set DATABASE_NAME_OVERRIDE="{DEMO_DB_PREFIX}" for this process.'
        )
        return 2
    if args.reset:
        reset_demo_schema(settings)
        print(f"demo database {name!r} reset and migrated to head")
    state, detail = check_schema(settings)
    if state != "ok":
        print(f"REFUSING: database schema not ready ({state}: {detail}) - run alembic upgrade head")
        return 2
    engine = get_engine(settings)
    with engine.connect() as conn:
        if conn.execute(text("select count(*) from companies")).scalar_one():
            print(f"database {name!r} already has companies - nothing to do (use --reset)")
            return 0

    qrun, decisions, erun = await seed(engine, settings)
    print(f"seeded SYNTHETIC demo data into database {name!r}:")
    for d in decisions:
        print(f"  {d.company_name:<16} decision={d.decision.value:<14} path={d.path.value}")
    print(
        f"  qualification: {qrun.qualified_count} qualified, {qrun.review_count} review, "
        f"{qrun.not_qualified_count} not qualified | enrichment: {erun.verified_count} verified "
        f"contact(s), {erun.blocked_count} blocked"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async()))
