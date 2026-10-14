"""Qualify companies: QUALIFIED / REVIEW / NOT_QUALIFIED (Phase 5 gate).

Default (no flags): DRY RUN - three demo companies from the labeled evaluation set, mock Laya,
in-memory store. No database, no network, no cost.

Options:
  --db          read real companies + Phase 4 signals from PostgreSQL (read-only by default)
  --persist     with --db: store the run and IMMUTABLE decisions (new rows; history kept)
  --live        use the local Laya sidecar (needs PROVIDER_MODE=live and a loopback
                LAYA_ENDPOINT_URL; free local compute - never a paid provider)
  --llm-review  route REVIEW cases to the LLM reviewer for this process (mock reviewer; the
                live reviewer adapter is a non-operational contract)
Never calls Apify, Firecrawl, Crawl4AI, Agent-Reach, Hunter, Apollo or enrichment-kit.

Examples:
  uv run python scripts/run_qualification.py
  uv run python scripts/run_qualification.py --db --limit 5
  uv run python scripts/run_qualification.py --db --live --persist --company-id <uuid>
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
from app.domain.qualification.records import QualificationDecisionRecord
from app.evaluation.qualification import case_input, load_dataset
from app.providers.base import LlmReviewProvider, QualificationProvider
from app.providers.laya import LayaAdapter, MockLayaProvider
from app.providers.registry import live_reasoning, mock_reasoning
from app.repositories.qualification import (
    InMemoryQualificationStore,
    QualificationStore,
    SqlQualificationStore,
)
from app.services.qualification import QualificationService

DEMO_CASES = ("Q19", "Q25", "Q03")  # qualified / review / not qualified


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--company-id", action="append", default=[], help="company UUID (repeatable)")
    p.add_argument("--limit", type=int, default=5, help="companies to evaluate (batch limit)")
    p.add_argument("--db", action="store_true", help="read real companies from PostgreSQL")
    p.add_argument("--persist", action="store_true", help="write decisions (requires --db)")
    p.add_argument("--live", action="store_true", help="use the local Laya sidecar")
    p.add_argument("--llm-review", action="store_true", help="enable REVIEW-case LLM review")
    p.add_argument("--json", action="store_true", help="print full decision records as JSON")
    return p.parse_args(argv)


def preconditions(args: argparse.Namespace, settings: Settings) -> list[str]:
    problems = []
    if args.persist and not args.db:
        problems.append("--persist requires --db")
    if args.limit < 1 or args.limit > 100:
        problems.append("--limit must be 1..100")
    if args.live and settings.provider_mode != ProviderMode.LIVE:
        problems.append("--live requires PROVIDER_MODE=live (set it for this process only)")
    if args.db:
        state, detail = check_schema(settings)
        if state != "ok":
            problems.append(f"database schema not ready ({state}: {detail})")
    return problems


def render(d: QualificationDecisionRecord) -> str:
    hard = ", ".join(f"{c['rule']}={c['outcome']}" for c in d.hard_rule_results)
    laya = d.laya_result or {}
    lines = [
        f"Company:          {d.company_name} ({d.company_id})",
        f"Verified domain:  {d.evidence_bundle.get('domain') or '-'} "
        f"[{d.evidence_bundle.get('domain_status')}]",
        f"Path:             {d.path.value}",
        f"Hard rules:       {hard}  (passed={d.hard_rules_passed})",
        f"Laya:             {d.laya_status.value}"
        + (f" verdict={laya.get('verdict')} trigger={laya.get('trigger')}" if laya else ""),
        f"Score/confidence: {d.score} / {d.confidence}",
        f"LLM review:       {d.llm_review_status.value}",
        f"FINAL DECISION:   {d.decision.value.upper()}",
        f"Evidence:         {d.signal_count} signals -> {d.event_count} events "
        f"({len(d.evidence_signal_ids)} signal ids in bundle)",
        "Reasons:",
        *[f"  - {r}" for r in d.reasons],
    ]
    return "\n".join(lines)


async def main_async(args: argparse.Namespace) -> int:
    settings = Settings()
    if args.llm_review:
        settings = settings.model_copy(update={"llm_review_enabled": True})
    configure_logging(level="WARNING", fmt="console", secret_values=settings.secret_values())
    problems = preconditions(args, settings)
    if problems:
        print("REFUSING:\n  - " + "\n  - ".join(problems))
        return 2

    laya: QualificationProvider
    if args.live:
        adapter = LayaAdapter(settings)
        health = await adapter.health()
        print(f"laya sidecar: {health.get('status')} loaded={health.get('loaded')}")
        laya = adapter
    else:
        laya = MockLayaProvider(settings)
    reviewer: LlmReviewProvider = (
        live_reasoning(settings) if args.live and not args.llm_review else mock_reasoning(settings)
    )

    store: QualificationStore
    ids: list[uuid.UUID] | None
    if args.db:
        store = SqlQualificationStore(get_engine(settings))
        ids = [uuid.UUID(c) for c in args.company_id] or None
        source = "PostgreSQL" + (" (PERSISTING decisions)" if args.persist else " (read-only)")
    else:
        dataset = load_dataset()
        memory = InMemoryQualificationStore()
        picked = [c for c in dataset.cases if c.id in DEMO_CASES]
        for case in picked:
            memory.add(case_input(case, dataset.reference_date))
        store, ids = memory, [i.facts.company_id for i in memory.inputs.values()]
        source = "DEMO (synthetic labeled cases, in-memory)"

    mode = "LIVE Laya sidecar" if args.live else "mock Laya"
    llm = "enabled" if settings.llm_review_enabled else "disabled"
    print(f"mode: {mode} | data: {source} | llm review: {llm}")
    service = QualificationService(store, laya, settings, reviewer=reviewer, persist=args.persist)
    run, decisions = await service.run(ids, limit=args.limit)
    for d in decisions:
        print("-" * 78)
        print(render(d))
    print("-" * 78)
    summary: dict[str, Any] = run.model_dump(
        mode="json",
        include={
            "id",
            "status",
            "policy_version",
            "laya_mode",
            "llm_review_mode",
            "companies_evaluated",
            "qualified_count",
            "review_count",
            "not_qualified_count",
        },
    )
    summary["persisted"] = args.persist
    print(json.dumps(summary, indent=2))
    if args.json:
        print(json.dumps([d.model_dump(mode="json") for d in decisions], indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(main_async(args))
    except AppError as exc:
        print(f"ERROR {exc.code}: {exc.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
