"""Research public business signals for VERIFIED companies (Phase 4, Agent-Reach).

Default (no ``--live``): DRY RUN - mock provider + in-memory store with one demo verified and
one demo unverified company (shows the gate). No network, no database, no cost.

Live run requires:
  * ``--live`` and ``PROVIDER_MODE=live`` (Agent-Reach channels are free, but they reach the
    internet, so they are never used implicitly);
  * at least one permitted channel available (news_rss always; youtube needs yt-dlp);
  * the database at the migration head.
Only VERIFIED companies are researched; others are recorded as SKIPPED. Budget per company:
SIGNAL_MAX_QUERIES_PER_COMPANY / SIGNAL_MAX_ITEMS_PER_COMPANY (flags can only lower them).

Examples:
  uv run python scripts/run_signal_research.py                          # dry run
  uv run python scripts/run_signal_research.py --live --limit 1 --max-queries 2 --max-items 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from typing import Any

from app.core.config import ProviderMode, Settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.db.session import check_schema, get_engine
from app.domain.enums import DomainResolutionStatus
from app.providers.agent_reach.adapter import AgentReachAdapter
from app.providers.agent_reach.mock import MockAgentReachProvider
from app.providers.base import SignalResearchProvider
from app.repositories.signals import (
    InMemorySignalStore,
    ResearchTarget,
    SignalStore,
    SqlSignalStore,
)
from app.services.signal_research import SignalResearchService


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--company-id", action="append", default=[], help="company UUID (repeatable)")
    p.add_argument("--limit", type=int, default=1, help="verified companies to research")
    p.add_argument("--max-queries", type=int, default=None, help="query budget per company")
    p.add_argument("--max-items", type=int, default=None, help="processed-item budget per company")
    p.add_argument("--live", action="store_true", help="really call Agent-Reach backends")
    return p.parse_args(argv)


def live_preconditions(settings: Settings, adapter: AgentReachAdapter) -> list[str]:
    problems = []
    if settings.provider_mode != ProviderMode.LIVE:
        problems.append("PROVIDER_MODE must be 'live'")
    if not adapter.available_channels():
        problems.append("no permitted Agent-Reach channel available (check AGENT_REACH_CHANNELS)")
    state, detail = check_schema(settings)
    if state != "ok":
        problems.append(f"database schema not ready ({state}: {detail})")
    return problems


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = Settings()
    configure_logging(level="INFO", fmt="console", secret_values=settings.secret_values())

    provider: SignalResearchProvider
    store: SignalStore
    if args.live:
        adapter = AgentReachAdapter(settings)
        problems = live_preconditions(settings, adapter)
        if problems:
            print("REFUSING live run:\n  - " + "\n  - ".join(problems))
            return 2
        provider, sql = adapter, SqlSignalStore(get_engine(settings))
        store = sql
        ids = [uuid.UUID(c) for c in args.company_id] or [
            t.company_id for t in sql.verified_targets(args.limit)
        ]
        print("channels:", ", ".join(c.value for c in adapter.available_channels()))
        mode = "LIVE"
    else:
        memory = InMemorySignalStore()
        verified = ResearchTarget(
            uuid.uuid4(),
            "Drivn",
            "drivn",
            "drivn.example",
            DomainResolutionStatus.VERIFIED,
            ("leasing", "commercial"),
        )
        unverified = ResearchTarget(
            uuid.uuid4(), "Iztri", "iztri", None, DomainResolutionStatus.UNRESOLVED
        )
        memory.add_target(verified)
        memory.add_target(unverified)
        provider, store = MockAgentReachProvider(settings), memory
        ids = [verified.company_id, unverified.company_id]
        mode = "DRY RUN (mock provider, in-memory store)"

    print(f"mode: {mode} | companies: {len(ids)}")
    service = SignalResearchService(provider, store, settings)
    summaries: list[dict[str, Any]] = []
    for company_id in ids:
        try:
            run = await service.run(
                company_id, max_queries=args.max_queries, max_items=args.max_items
            )
        except AppError as exc:
            print(f"company {company_id} FAILED: {exc.code}: {exc.message}")
            continue
        summaries.append(
            run.model_dump(
                mode="json",
                include={
                    "company_id",
                    "status",
                    "queries_used",
                    "max_queries",
                    "items_processed",
                    "max_items",
                    "items_dropped",
                    "signals_created",
                    "signals_duplicate",
                    "channels_used",
                    "error_message",
                    "queries",
                },
            )
        )
        for signal in store.list_signals(company_id):
            print(f"  [{signal.signal_type.value}] conf={signal.confidence} {signal.summary[:90]}")
    print(json.dumps(summaries, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
