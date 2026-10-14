"""AI lead analysis / offer angle for ELIGIBLE leads (Phase 7).

Eligible = latest qualification decision QUALIFIED (current policy + ICP config) AND a VERIFIED
Phase 6 contact enriched under that decision. Everything else is BLOCKED (no model call).

Default (no flags): DRY RUN - three synthetic companies qualified + enriched by the real
Phase 5/6 services with mocks, analysed by the mock AI provider. No DB, no network, no cost.

Options:
  --db        real eligible companies from PostgreSQL, mock AI, nothing written
  --persist   with --db: store the run and IMMUTABLE analysis records
  --live      OpenAI (paid, budgeted). Needs --db, PROVIDER_MODE=live,
              ALLOW_PAID_PROVIDER_CALLS=true and OPENAI_API_KEY. Always persists
              (usage/cost history).

Examples:
  uv run python scripts/run_lead_analysis.py
  uv run python scripts/run_lead_analysis.py --db --limit 5
  uv run python scripts/run_lead_analysis.py --db --live --limit 1
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
from app.demo import build_demo_world, demo_settings
from app.domain.analysis.records import LeadAnalysisRecord
from app.providers.base import ReasoningProvider
from app.providers.registry import live_reasoning, mock_reasoning
from app.repositories.analysis import AnalysisStore, InMemoryAnalysisStore, SqlAnalysisStore
from app.repositories.enrichment import EnrichmentStore, SqlEnrichmentStore
from app.repositories.qualification import QualificationStore, SqlQualificationStore
from app.services.lead_analysis import LeadAnalysisService


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--company-id", action="append", default=[], help="company UUID (repeatable)")
    p.add_argument("--limit", type=int, default=5, help="companies per run (batch limit)")
    p.add_argument("--db", action="store_true", help="read real data from PostgreSQL")
    p.add_argument("--persist", action="store_true", help="write results (requires --db)")
    p.add_argument("--live", action="store_true", help="call OpenAI (paid)")
    p.add_argument("--json", action="store_true", help="print full analysis records as JSON")
    return p.parse_args(argv)


def preconditions(args: argparse.Namespace, settings: Settings) -> list[str]:
    problems = []
    if args.persist and not args.db:
        problems.append("--persist requires --db")
    if args.limit < 1 or args.limit > 50:
        problems.append("--limit must be 1..50")
    if args.live:
        if not args.db:
            problems.append("--live requires --db (usage and cost must be persisted)")
        if settings.provider_mode != ProviderMode.LIVE:
            problems.append("--live requires PROVIDER_MODE=live (set it for this process only)")
        if not settings.allow_paid_provider_calls:
            problems.append("--live requires ALLOW_PAID_PROVIDER_CALLS=true (this process only)")
        if not settings.provider_has_credentials(live_reasoning(settings).name):
            problems.append("OPENAI_API_KEY is not set in .env (see docs/OPENAI_PROVIDER_NOTES.md)")
    if args.db:
        state, detail = check_schema(settings)
        if state != "ok":
            problems.append(f"database schema not ready ({state}: {detail})")
    return problems


def render(r: LeadAnalysisRecord) -> str:
    a = r.analysis or {}
    contact = (r.evidence_bundle.get("contact") or {}) if r.evidence_bundle else {}
    facts = a.get("observed_facts") or []
    hyps = a.get("pain_hypotheses") or []
    opps = a.get("ai_opportunities") or []
    lines = [
        f"Company:            {r.company_name}",
        f"Contact:            {contact.get('name') or '-'} ({contact.get('title') or '-'})",
        f"Status:             {r.status.value.upper()}"
        + (f"  [{r.error_code}: {r.error_message}]" if r.error_code else ""),
        f"Primary trigger:    {r.primary_trigger or '-'}",
        "Observed facts:     " + (" | ".join(f["statement"] for f in facts[:3]) or "-"),
        "Pain hypothesis:    "
        + (f"{hyps[0]['statement']} (conf {hyps[0]['confidence']})" if hyps else "-"),
        "Opportunity:        " + (opps[0]["description"] if opps else "-"),
        f"Recommended offer:  {r.recommended_offer_id or '-'}",
        f"Offer reason:       {a.get('offer_match_reason', '-')}",
        f"Outreach hook:      {a.get('outreach_hook', '-')}",
        f"Confidence:         {r.confidence if r.confidence is not None else '-'}",
        f"Evidence count:     {len(r.supporting_evidence_ids)} cited / "
        f"{len((r.evidence_bundle or {}).get('events', []))} events in bundle",
        f"Model/provider:     {r.model or '-'} via {r.gateway or '-'}/{r.provider or '-'}"
        f"  tokens={r.usage.get('total_tokens', 0)} "
        f"cost=${r.cost_usd if r.cost_usd is not None else '-'}",
    ]
    return "\n".join(lines)


async def main_async(args: argparse.Namespace) -> int:
    settings = Settings()
    configure_logging(level="WARNING", fmt="console", secret_values=settings.secret_values())
    problems = preconditions(args, settings)
    if problems:
        print("REFUSING:\n  - " + "\n  - ".join(problems))
        return 2

    provider: ReasoningProvider
    if args.live:
        adapter = live_reasoning(settings)
        status = adapter.status()
        print(f"reasoning provider: {status.name.value} | {status.detail}")
        provider = adapter
    else:
        provider = mock_reasoning(settings)

    astore: AnalysisStore
    qstore: QualificationStore
    estore: EnrichmentStore
    ids: list[uuid.UUID] | None
    if args.db:
        engine = get_engine(settings)
        astore, qstore, estore = (
            SqlAnalysisStore(engine),
            SqlQualificationStore(engine),
            SqlEnrichmentStore(engine),
        )
        ids = [uuid.UUID(c) for c in args.company_id] or None
        persist = args.persist or args.live
        source = "PostgreSQL" + (" (PERSISTING)" if persist else " (read-only)")
    else:
        world = await build_demo_world(settings)
        settings = demo_settings(settings)  # synthetic data: mock AI is free
        astore, qstore, estore = InMemoryAnalysisStore(), world.qualifications, world.enrichment
        ids, persist, source = world.company_ids, True, "DEMO (synthetic, in-memory)"

    print(f"mode: {'LIVE OpenAI' if args.live else 'mock AI'} | data: {source}")
    service = LeadAnalysisService(astore, qstore, estore, provider, settings, persist=persist)
    run, records = await service.run(ids, limit=args.limit)
    if not records:
        print("no eligible lead (needs latest decision QUALIFIED + a VERIFIED contact under it)")
    for r in records:
        print("-" * 78)
        print(render(r))
    print("-" * 78)
    summary: dict[str, Any] = run.model_dump(
        mode="json",
        include={
            "id",
            "status",
            "mode",
            "policy_version",
            "prompt_version",
            "catalog_version",
            "analyses_created",
            "succeeded_count",
            "rejected_count",
            "failed_count",
            "blocked_count",
            "total_tokens",
            "cost_usd",
            "error_code",
        },
    )
    summary["persisted"] = persist and args.db
    print(json.dumps(summary, indent=2))
    if args.json:
        print(json.dumps([r.model_dump(mode="json") for r in records], indent=2))
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
