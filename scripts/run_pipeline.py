"""Run ONE pipeline: the existing stages in order, each through its own gate and budget
(Phase 9). discovery -> website -> signals -> qualification -> enrichment -> analysis -> export

Default (no flags): DRY RUN on the offline demo (synthetic companies, mock providers, nothing
written anywhere, no network).

Options:
  --db         real data from PostgreSQL. Still a DRY RUN (no stage writes); the pipeline run
               itself is recorded so it shows in the dashboard.
  --live       a WRITING run. Needs --db and PROVIDER_MODE=live for this process. Paid stages
               also need ALLOW_PAID_PROVIDER_CALLS=true; the Sheet is written only with
               ALLOW_SHEETS_WRITES=true. Gates and budgets are never bypassed.
  --stages     comma list (default: PIPELINE_STAGES), e.g. --stages analysis,export.
               ``all`` = every stage INCLUDING discovery (which spends Apify credit).
  --owner NAME team member (LEAD_OWNERS) who owns every lead this run produces
  --prompt ".." search queries for THIS run's discovery (';' separated) instead of the
               configured DISCOVERY_* query sets
  --limit N    companies per stage (1..PIPELINE_MAX_COMPANY_LIMIT; refused beyond, never
               silently reduced). Default: PIPELINE_COMPANY_LIMIT
  --status     list recent pipeline runs and exit
  --recover    close a stale RUNNING run (no heartbeat) and exit

Only one pipeline can run at a time; a second trigger is refused.

Examples:
  uv run python scripts/run_pipeline.py
  uv run python scripts/run_pipeline.py --db
  uv run python scripts/run_pipeline.py --db --status
  uv run python scripts/run_pipeline.py --db --live --stages analysis,export --limit 1
  uv run python scripts/run_pipeline.py --db --live --stages all --limit 10
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys

from app.core.config import ProviderMode, Settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.db.session import check_schema, get_engine
from app.demo import DemoOutputs, build_demo_outputs, demo_pipeline
from app.domain.pipeline import PipelineAlreadyRunningError, PipelineRunRecord, Trigger
from app.providers.registry import ProviderRegistry
from app.repositories.outputs import SqlOutputStore
from app.repositories.pipeline import SqlPipelineStore
from app.services.credits import CreditService
from app.services.pipeline import PipelineService, parse_stages, run_summary
from app.services.pipeline_stages import sql_stages
from app.services.sheets_export import FIXTURE_LABEL


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--db", action="store_true", help="real data from PostgreSQL")
    p.add_argument("--live", action="store_true", help="writing run (needs --db + live mode)")
    p.add_argument("--stages", default=None, help="comma list of stages")
    p.add_argument("--limit", type=int, default=None, help="companies per stage")
    p.add_argument("--owner", default=None, help="team member who gets the leads of this run")
    p.add_argument("--prompt", default=None, help="search queries for discovery (';' separated)")
    p.add_argument("--status", action="store_true", help="list recent runs and exit")
    p.add_argument("--recover", action="store_true", help="close a stale running run and exit")
    p.add_argument("--json", action="store_true", help="print the run as JSON")
    return p.parse_args(argv)


def preconditions(args: argparse.Namespace, settings: Settings) -> list[str]:
    problems = []
    ceiling = settings.pipeline_max_company_limit
    if args.limit is not None and not 1 <= args.limit <= ceiling:
        problems.append(f"--limit must be 1..{ceiling} (PIPELINE_MAX_COMPANY_LIMIT)")
    if (args.live or args.status or args.recover) and not args.db:
        problems.append("--live / --status / --recover require --db")
    if args.live and settings.provider_mode != ProviderMode.LIVE:
        problems.append("--live requires PROVIDER_MODE=live (set it for this process only)")
    if args.db:
        state, detail = check_schema(settings)
        if state != "ok":
            problems.append(f"database schema not ready ({state}: {detail})")
    return problems


def render(run: PipelineRunRecord) -> str:
    kind = "DRY RUN (nothing written)" if run.dry_run else "LIVE (writing)"
    lines = [
        f"pipeline run {run.id}",
        f"  status: {run.status.value.upper()}   mode: {run.mode}   {kind}   "
        f"limit: {run.company_limit}   estimated cost: ${run.estimated_cost_usd}",
    ]
    if run.error_code:
        lines.append(f"  error: {run.error_code}: {run.error_message}")
    for s in run.stages:
        counts = ", ".join(f"{k} {v}" for k, v in s.counts.items()) or "-"
        note = f"  [{s.detail}]" if s.detail else ""
        err = f"  ERROR {s.error_code}: {s.error_message}" if s.error_code else ""
        lines.append(
            f"  {s.stage.value:<14} {s.status.value:<10} {s.duration_ms:>6} ms  {counts}{note}{err}"
        )
    not_run = [s.value for s in run.stages_requested if run.result(s) is None]
    if not_run:
        lines.append(f"  not run: {', '.join(not_run)}")
    return "\n".join(lines)


def render_demo_leads(demo: DemoOutputs) -> str:
    """The synthetic demo leads after the run, one line per company (every name labelled)."""
    lines = ["demo leads (all SYNTHETIC):"]
    for company_id in demo.world.company_ids:
        view = demo.views.view(company_id)
        if view is None:
            continue
        contact = view.contact
        who = f"{contact.full_name} ({contact.title})" if contact is not None else "-"
        state = view.export_status.value if view.in_sheet else "not in the sheet"
        lines.append(
            f"  {FIXTURE_LABEL}{view.company_name:<14} {view.decision.decision.value:<14}"
            f" {state:<17} {who}"
        )
    return "\n".join(lines)


async def main_async(args: argparse.Namespace) -> int:
    settings = Settings()
    configure_logging(level="WARNING", fmt="console", secret_values=settings.secret_values())
    problems = preconditions(args, settings)
    if problems:
        print("REFUSING:\n  - " + "\n  - ".join(problems))
        return 2
    stages = parse_stages(args.stages) if args.stages else None

    demo: DemoOutputs | None = None
    if args.db:
        engine = get_engine(settings)
        registry = ProviderRegistry(settings)
        credits = CreditService(
            settings,
            SqlOutputStore(engine),
            hunter_usage=getattr(registry.enrichment, "account_usage", None),
        )
        service = PipelineService(
            SqlPipelineStore(engine),
            sql_stages(settings, engine, registry),
            settings,
            credit_snapshot=credits.lines,  # the dashboard shows what this run spent
        )
        source = "PostgreSQL"
    else:
        demo = await build_demo_outputs(settings)
        service = demo_pipeline(settings, demo)
        source = "DEMO (synthetic, in-memory)"

    if args.status:
        for run in await service.runs(10):
            print(render(run))
        return 0
    if args.recover:
        recovered = await service.recover_stale()
        print(f"recovered {len(recovered)} stale run(s)")
        for run in recovered:
            print(render(run))
        return 0

    print(
        f"mode: {'LIVE (writing)' if args.live else 'DRY RUN'} | data: {source} | "
        f"providers: {service.mode} | paid calls: "
        f"{'allowed' if settings.allow_paid_provider_calls else 'blocked'} | sheet writes: "
        f"{'allowed' if settings.allow_sheets_writes else 'blocked'}"
    )
    try:
        run = await service.run(
            trigger=Trigger.CLI,
            dry_run=not args.live,
            stages=stages,
            limit=args.limit,
            requested_by=args.owner or getpass.getuser(),
            owner=args.owner,
            prompt=(args.prompt or "").replace(";", "\n") or None,
        )
    except PipelineAlreadyRunningError as exc:
        print(f"REFUSING: a pipeline run is already in progress ({exc.running_id}).")
        print("  If that process died, close it with:  --db --recover")
        return 3
    print(render(run))
    if demo is not None:
        print(render_demo_leads(demo))
    if args.json:
        print(json.dumps(run_summary(run), indent=2))
    return 0 if run.status.value == "succeeded" else 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(main_async(args))
    except AppError as exc:
        print(f"ERROR {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted - the run was recorded as CANCELLED", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
