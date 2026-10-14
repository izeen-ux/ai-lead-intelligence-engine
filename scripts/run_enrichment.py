"""Enrich QUALIFIED companies: person identity -> cache -> enrichment-kit (Hunter -> Apollo) ->
email verification -> append-only results (Phase 6).

Default (no flags): DRY RUN - two synthetic companies qualified by the REAL Phase 5 service
(mock Laya) plus one REVIEW company, mock enrichment providers, in-memory stores.
No database, no network, no cost. The REVIEW company is BLOCKED by the gate.

Options:
  --db             real QUALIFIED companies from PostgreSQL, mock providers, nothing written
  --persist        with --db: write run / contacts / attempts / cache
  --live           real calls through the local enrichment sidecar (Hunter/Apollo credits!).
                   Needs --db, PROVIDER_MODE=live, ALLOW_PAID_PROVIDER_CALLS=true and a
                   reachable ENRICHMENT_KIT_BASE_URL. Always persists (budgets need history).
  --people-search  hunter: Hunter Domain Search when the company's pages name nobody
The qualification gate, budgets and secret redaction apply in every mode.

Examples:
  uv run python scripts/run_enrichment.py
  uv run python scripts/run_enrichment.py --db --limit 5
  uv run python scripts/run_enrichment.py --db --live --limit 1
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
from app.domain.enrichment.identity import PageText
from app.domain.enrichment.records import EnrichmentContactRecord
from app.domain.enums import DomainResolutionStatus
from app.evaluation.qualification import case_input, load_dataset
from app.providers.base import EmailVerifier, EnrichmentOrchestrator
from app.providers.enrichment_kit import EnrichmentKitAdapter, MockEnrichmentKitProvider
from app.providers.hunter import HunterAdapter, MockHunterProvider
from app.providers.laya import MockLayaProvider
from app.repositories.enrichment import (
    CompanyContext,
    EnrichmentStore,
    InMemoryEnrichmentStore,
    SqlEnrichmentStore,
)
from app.repositories.qualification import (
    InMemoryQualificationStore,
    QualificationStore,
    SqlQualificationStore,
)
from app.services.enrichment import EnrichmentService
from app.services.qualification import QualificationService

# (labeled case id, synthetic team-page markdown on the company's own domain)
DEMO = {
    "Q19": "## Our team\n### Riya Kapoor - Founder & CEO\nRiya Kapoor is the CEO of Quikcart.",
    "Q01": "## Leadership\n### Arjun Missra - Co-Founder & CEO\n### Meena Iyer - Head of Growth",
    "Q25": "### Dev Rao - Founder & CEO",  # REVIEW in Phase 5 -> blocked by the gate
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--company-id", action="append", default=[], help="company UUID (repeatable)")
    p.add_argument("--limit", type=int, default=5, help="companies per run (batch limit)")
    p.add_argument("--db", action="store_true", help="read real data from PostgreSQL")
    p.add_argument("--persist", action="store_true", help="write results (requires --db)")
    p.add_argument("--live", action="store_true", help="real Hunter/Apollo calls via the sidecar")
    p.add_argument("--people-search", choices=["none", "hunter"], default=None)
    p.add_argument("--json", action="store_true", help="print full contact records as JSON")
    return p.parse_args(argv)


def preconditions(args: argparse.Namespace, settings: Settings) -> list[str]:
    problems = []
    if args.persist and not args.db:
        problems.append("--persist requires --db")
    if args.limit < 1 or args.limit > 50:
        problems.append("--limit must be 1..50")
    if args.live:
        if not args.db:
            problems.append("--live requires --db (results and budgets must be persisted)")
        if settings.provider_mode != ProviderMode.LIVE:
            problems.append("--live requires PROVIDER_MODE=live (set it for this process only)")
        if not settings.allow_paid_provider_calls:
            problems.append("--live requires ALLOW_PAID_PROVIDER_CALLS=true (this process only)")
    if args.db:
        state, detail = check_schema(settings)
        if state != "ok":
            problems.append(f"database schema not ready ({state}: {detail})")
    return problems


async def demo_stores(
    settings: Settings,
) -> tuple[EnrichmentStore, QualificationStore, list[uuid.UUID]]:
    """Qualify the demo companies with the REAL Phase 5 service (mock Laya) - the gate is never
    bypassed, even in the demo."""
    dataset = load_dataset()
    qstore = InMemoryQualificationStore()
    estore = InMemoryEnrichmentStore()
    ids = []
    for case in dataset.cases:
        if case.id not in DEMO:
            continue
        data = case_input(case, dataset.reference_date)
        qstore.add(data)
        estore.add_company(
            CompanyContext(
                company_id=data.facts.company_id,
                name=data.facts.name,
                domain=data.facts.domain,
                domain_status=DomainResolutionStatus(data.facts.domain_status),
                pages=(PageText(url=f"https://{data.facts.domain}/team", markdown=DEMO[case.id]),),
            )
        )
        ids.append(data.facts.company_id)
    await QualificationService(qstore, MockLayaProvider(settings), settings).run(ids)
    return estore, qstore, ids


def _attempt_summary(c: EnrichmentContactRecord, provider: str) -> str:
    return str((c.provenance.get("waterfall") or {}).get(provider, "-"))


def render(c: EnrichmentContactRecord) -> str:
    lines = [
        f"Company:          {c.company_name} ({c.domain or '-'})",
        f"Qualification ID: {c.qualification_id or '- (gate refused)'}",
        f"Person:           {c.full_name or '-'}  | Title: {c.title or '-'}",
        f"Hunter result:    {_attempt_summary(c, 'hunter')}",
        f"Apollo result:    {_attempt_summary(c, 'apollo')}",
        f"Email:            {c.email or '-'}",
        f"Verification:     {c.email_verification.value}"
        + (f" (by {c.verification_provider.value})" if c.verification_provider else ""),
        f"Confidence:       {c.confidence if c.confidence is not None else '-'}",
        f"Provider:         {c.provider.value if c.provider else '-'}"
        + ("  [from cache]" if c.from_cache else ""),
        f"Cost:             credits {c.credits_used or {}} ~ ${c.estimated_cost_usd}",
        f"FINAL STATUS:     {c.status.value.upper()}",
    ]
    lines += [f"  - {r}" for r in c.reasons]
    return "\n".join(lines)


async def main_async(args: argparse.Namespace) -> int:
    settings = Settings()
    if args.people_search:
        settings = settings.model_copy(update={"enrichment_people_search": args.people_search})
    configure_logging(level="WARNING", fmt="console", secret_values=settings.secret_values())
    problems = preconditions(args, settings)
    if problems:
        print("REFUSING:\n  - " + "\n  - ".join(problems))
        return 2

    orchestrator: EnrichmentOrchestrator
    verifier: EmailVerifier
    before: dict[str, Any] = {}
    if args.live:
        kit = EnrichmentKitAdapter(settings)
        health = await kit.health()
        print(
            f"sidecar: {health.get('status')} enrichment-kit "
            f"{(health.get('enrichmentKit') or {}).get('version')} "
            f"providers={json.dumps(health.get('providers'))}"
        )
        orchestrator, verifier = kit, HunterAdapter(settings)
        before = await kit.account_usage()
    else:
        orchestrator, verifier = MockEnrichmentKitProvider(settings), MockHunterProvider(settings)

    ids: list[uuid.UUID] | None
    if args.db:
        engine = get_engine(settings)
        estore: EnrichmentStore = SqlEnrichmentStore(engine)
        qstore: QualificationStore = SqlQualificationStore(engine)
        ids = [uuid.UUID(c) for c in args.company_id] or None
        persist = args.persist or args.live
        source = "PostgreSQL" + (" (PERSISTING)" if persist else " (read-only)")
    else:
        estore, qstore, ids = await demo_stores(settings)
        persist, source = True, "DEMO (synthetic, in-memory)"

    mode = "LIVE sidecar (paid)" if args.live else "mock providers"
    print(f"mode: {mode} | data: {source}")
    search = None
    if args.live and settings.enrichment_web_search:
        from app.providers.registry import ProviderRegistry

        search = ProviderRegistry(settings).web_search
    service = EnrichmentService(
        estore, qstore, orchestrator, verifier, settings, persist=persist, web_search=search
    )
    run, contacts = await service.run(ids, limit=args.limit)
    if not contacts:
        print("no QUALIFIED company to enrich (the gate only admits the latest QUALIFIED decision)")
    for c in contacts:
        print("-" * 78)
        print(render(c))
    print("-" * 78)
    summary: dict[str, Any] = run.model_dump(
        mode="json",
        include={
            "id", "status", "mode", "policy_version", "companies_evaluated", "contacts_created",
            "verified_count", "cached_count", "blocked_count", "failed_count", "hunter_credits",
            "apollo_credits", "sidecar_calls", "estimated_cost_usd", "error_code",
        },
    )  # fmt: skip
    summary["persisted"] = persist and args.db
    if args.live:
        after = await kit.account_usage()
        summary["hunter_account_usage"] = {
            "before": before.get("requests"),
            "after": after.get("requests"),
            "note": "Hunter's own counters (free endpoint) - the source of truth for spend",
        }
    print(json.dumps(summary, indent=2))
    if args.json:
        print(json.dumps([c.model_dump(mode="json") for c in contacts], indent=2))
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
