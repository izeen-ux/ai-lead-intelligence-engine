"""Run one discovery request.

Default (no ``--live``): DRY RUN - mock provider + in-memory store. No network, no database,
no cost.

Live run requires ALL of:
  * ``--live`` on the command line,
  * ``PROVIDER_MODE=live`` and ``ALLOW_PAID_PROVIDER_CALLS=true`` in the environment/.env,
  * a reachable database at the current migration head.
Spend is additionally capped by APIFY_MAX_ITEMS_PER_RUN / APIFY_MAX_CHARGE_USD_PER_RUN
(requests can only lower these).

Examples:
  uv run python scripts/run_discovery.py --keyword "startup raises seed funding crore" --limit 5
  uv run python scripts/run_discovery.py --live --limit 5 --max-items 1 --max-charge-usd 0.50
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from decimal import Decimal
from typing import Any

from app.core.config import ProviderMode, Settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.db.session import check_schema, get_engine
from app.domain.sources import SourceRegistry
from app.providers.apify.mock import MockApifyProvider
from app.providers.base import DiscoveryProvider
from app.providers.contracts import DiscoveryQuery
from app.providers.registry import ProviderRegistry
from app.repositories.discovery import (
    DiscoveryStore,
    InMemoryDiscoveryStore,
    SqlDiscoveryStore,
)
from app.services.discovery import DiscoveryService


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--keyword", action="append", default=[], help="search query (repeatable)")
    p.add_argument("--country", action="append", default=[], help="ISO country code, e.g. IN")
    p.add_argument("--source", default="funding_news")
    p.add_argument("--limit", type=int, default=5, help="max candidates kept (default 5)")
    p.add_argument("--max-items", type=int, default=None, help="max Actor dataset items")
    p.add_argument(
        "--max-charge-usd",
        type=Decimal,
        default=None,
        help="Apify hard cap for this run (Apify minimum 0.50)",
    )
    p.add_argument("--quick-date-range", default=None, help="e.g. m1 (past month), w2, d10")
    p.add_argument("--live", action="store_true", help="really call Apify (costs credits)")
    return p.parse_args(argv)


def build_query(args: argparse.Namespace) -> DiscoveryQuery:
    params: dict[str, Any] = {}
    if args.quick_date_range:
        params["quickDateRange"] = args.quick_date_range
    return DiscoveryQuery(
        source=args.source,
        keywords=args.keyword,
        country_codes=args.country or ["IN"],
        limit=args.limit,
        max_items=args.max_items,
        max_charge_usd=args.max_charge_usd,
        params=params,
    )


def live_preconditions(settings: Settings) -> list[str]:
    problems = []
    if settings.provider_mode != ProviderMode.LIVE:
        problems.append("PROVIDER_MODE must be 'live'")
    if not settings.allow_paid_provider_calls:
        problems.append("ALLOW_PAID_PROVIDER_CALLS must be true")
    state, detail = check_schema(settings)
    if state != "ok":
        problems.append(f"database schema not ready ({state}: {detail})")
    return problems


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = Settings()
    configure_logging(level="INFO", fmt="console", secret_values=settings.secret_values())
    query = build_query(args)

    provider: DiscoveryProvider
    store: DiscoveryStore
    if args.live:
        problems = live_preconditions(settings)
        if problems:
            print("REFUSING live run:\n  - " + "\n  - ".join(problems))
            return 2
        provider = ProviderRegistry(settings).discovery
        store = SqlDiscoveryStore(get_engine(settings))
        mode = "LIVE"
    else:
        provider = MockApifyProvider(settings)
        store = InMemoryDiscoveryStore()
        mode = "DRY RUN (mock provider, in-memory store)"

    print(f"mode: {mode}")
    try:
        sources = SourceRegistry.from_setting(settings.discovery_sources_enabled)
        run = await DiscoveryService(provider, store, sources).run(query)
    except AppError as exc:
        print(f"discovery FAILED: {exc.code}: {exc.message}")
        return 1

    summary = run.model_dump(
        mode="json",
        include={
            "id",
            "status",
            "provider",
            "actor_id",
            "provider_run_id",
            "provider_status",
            "max_items",
            "max_charge_usd",
            "started_at",
            "finished_at",
            "raw_item_count",
            "candidate_count",
            "new_company_count",
            "duplicate_count",
            "skipped_count",
            "estimated_cost_usd",
            "cost_usd",
            "charged_events",
        },
    )
    print(json.dumps(summary, indent=2))
    print("candidate pool (most recent first):")
    for company in store.list_companies(limit=args.limit):
        print(f"  - {company.name}  (sources: {len(company.source_ids)})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
