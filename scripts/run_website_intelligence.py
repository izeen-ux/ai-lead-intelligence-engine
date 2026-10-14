"""Resolve official domains and crawl company websites (Phase 3).

Default (no ``--live``): DRY RUN with mock providers and an in-memory demo company. No network,
no database, no cost.

Live run requires:
  * ``--live`` on the command line and ``PROVIDER_MODE=live``;
  * ``ALLOW_PAID_PROVIDER_CALLS=true`` whenever Firecrawl may be called (i.e. unless the run is a
    fallback-only crawl of an operator-supplied ``--domain``);
  * the database at the migration head.
Spend is capped by ``--max-credits`` (<= FIRECRAWL_MAX_CREDITS_PER_RUN) and per company by
FIRECRAWL_MAX_CREDITS_PER_COMPANY / WEBSITE_MAX_PAGES_PER_COMPANY.

Examples:
  uv run python scripts/run_website_intelligence.py                       # dry run
  uv run python scripts/run_website_intelligence.py --live --limit 1 --pages 3 --max-credits 10
  uv run python scripts/run_website_intelligence.py --live --company-id <uuid> \\
      --domain example.com --force-fallback --pages 2               # Crawl4AI only (free)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from dataclasses import asdict
from typing import Any

from app.core.config import ProviderMode, Settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.db.session import check_schema, get_engine
from app.domain.enums import DomainResolutionStatus
from app.providers.firecrawl.adapter import FirecrawlAdapter
from app.providers.registry import ProviderRegistry
from app.repositories.website import (
    CompanyContext,
    InMemoryWebsiteStore,
    SqlWebsiteStore,
    WebsiteStore,
)
from app.services.website_intelligence import CreditBudget, WebsiteIntelligenceService


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--company-id", action="append", default=[], help="company UUID (repeatable)")
    p.add_argument("--limit", type=int, default=1, help="unresolved companies to process")
    p.add_argument("--domain", default=None, help="operator-supplied domain (one company only)")
    p.add_argument("--pages", type=int, default=None, help="page budget per company")
    p.add_argument("--max-credits", type=int, default=None, help="Firecrawl credit budget for run")
    p.add_argument("--force-fallback", action="store_true", help="skip Firecrawl crawl (Crawl4AI)")
    p.add_argument("--country", default="IN")
    p.add_argument("--live", action="store_true", help="really call providers")
    return p.parse_args(argv)


def live_preconditions(settings: Settings, args: argparse.Namespace) -> list[str]:
    problems = []
    if settings.provider_mode != ProviderMode.LIVE:
        problems.append("PROVIDER_MODE must be 'live'")
    firecrawl_needed = not (args.force_fallback and args.domain)
    if firecrawl_needed and not settings.allow_paid_provider_calls:
        problems.append("ALLOW_PAID_PROVIDER_CALLS must be true (Firecrawl is paid)")
    if args.force_fallback and not settings.crawl4ai_base_url:
        problems.append("CRAWL4AI_BASE_URL must be set for a fallback crawl")
    state, detail = check_schema(settings)
    if state != "ok":
        problems.append(f"database schema not ready ({state}: {detail})")
    return problems


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = Settings()
    configure_logging(level="INFO", fmt="console", secret_values=settings.secret_values())
    if args.domain and len(args.company_id) != 1:
        print("--domain requires exactly one --company-id")
        return 2

    store: WebsiteStore
    if args.live:
        problems = live_preconditions(settings, args)
        if problems:
            print("REFUSING live run:\n  - " + "\n  - ".join(problems))
            return 2
        registry = ProviderRegistry(settings)
        store = SqlWebsiteStore(get_engine(settings))
        company_ids = [uuid.UUID(c) for c in args.company_id] or store.companies_needing_resolution(
            args.limit
        )
        mode = "LIVE"
    else:
        registry = ProviderRegistry(
            settings.model_copy(update={"provider_mode": ProviderMode.MOCK})
        )
        memory = InMemoryWebsiteStore()
        demo = CompanyContext(
            company_id=uuid.uuid4(),
            name="Acme Robotics",
            normalized_name="acme robotics",
            domain=None,
            domain_status=DomainResolutionStatus.UNRESOLVED,
            publisher_hosts=frozenset({"news.example"}),
        )
        memory.add_company(demo)
        store = memory
        company_ids = [demo.company_id]
        mode = "DRY RUN (mock providers, in-memory store)"

    run_cap = settings.firecrawl_max_credits_per_run
    budget = CreditBudget(total=min(args.max_credits or run_cap, run_cap))
    print(f"mode: {mode} | companies: {len(company_ids)} | credit budget: {budget.total}")

    firecrawl = registry.website if isinstance(registry.website, FirecrawlAdapter) else None
    before = await _balance(firecrawl) if firecrawl and settings.allow_paid_provider_calls else None
    service = WebsiteIntelligenceService(
        search=registry.web_search,
        primary=registry.website,
        fallback=registry.website_fallback,
        store=store,
        settings=settings,
    )
    results: list[dict[str, Any]] = []
    for company_id in company_ids:
        try:
            outcome = await service.process(
                company_id,
                budget,
                domain_override=args.domain,
                page_budget=args.pages,
                force_fallback=args.force_fallback,
                country=args.country,
            )
        except AppError as exc:
            print(f"company {company_id} FAILED: {exc.code}: {exc.message}")
            continue
        results.append(json.loads(json.dumps(asdict(outcome), default=str)))
    after = await _balance(firecrawl) if firecrawl and settings.allow_paid_provider_calls else None

    print(json.dumps(results, indent=2))
    print(f"run credits used (adapter-counted): {budget.spent}")
    if before is not None and after is not None:
        print(f"Firecrawl remaining credits: before={before} after={after} delta={before - after}")
    return 0


async def _balance(firecrawl: FirecrawlAdapter) -> int | None:
    try:
        return await firecrawl.remaining_credits()
    except AppError:
        return None


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
