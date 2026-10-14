"""Export the Google Sheets projection of PostgreSQL state: ONE tab ("Ai Leads").

One row per company whose latest decision is QUALIFIED, or REVIEW with passing hard ICP rules
(column ``lead_status``: qualified / needs review). Upsert by lead_id; nothing is duplicated.
The Sheet is OUTPUT only: edits made in it never flow back into the database.

Default (no flags): DRY RUN on the offline demo (synthetic companies). No database, no Google
call, nothing written. Synthetic data is NEVER written to Google Sheets.

Options:
  --db         real data from PostgreSQL (still a dry run unless --live)
  --check      read the real Sheet (credentials, workbook, tab, current rows) and plan the
               upsert against it - writes NOTHING
  --live       write to the real Sheet. Needs --db, ALLOW_SHEETS_WRITES=true (set for this
               process only), GOOGLE_SHEETS_SPREADSHEET_ID and the service-account key file.
               Always records export history in PostgreSQL. Refused for the demo database.
  --persist    with --db in a dry run: also store the (dry_run) export history
  --limit N    companies read (default 25); EXPORT_MAX_ROWS_PER_RUN caps row writes

Examples:
  uv run python scripts/export_leads_to_sheets.py
  uv run python scripts/export_leads_to_sheets.py --db --limit 5
  uv run python scripts/export_leads_to_sheets.py --db --check
  uv run python scripts/export_leads_to_sheets.py --db --live
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.db.session import check_schema, get_engine
from app.demo import build_demo_outputs
from app.domain.outputs.records import ExportMode
from app.providers.google_sheets import (
    GoogleSheetsWriter,
    ServiceAccountTokenProvider,
    SheetsWriter,
    service_account_email,
)
from app.repositories.analysis import SqlAnalysisStore
from app.repositories.enrichment import SqlEnrichmentStore
from app.repositories.outputs import OutputStore, SqlOutputStore
from app.repositories.qualification import SqlQualificationStore
from app.services.leads import LeadViewService
from app.services.sheets_export import (
    DEMO_DB_PREFIX,
    ExportReport,
    SheetsExportService,
    output_profile,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--db", action="store_true", help="read real data from PostgreSQL")
    p.add_argument("--dry-run", action="store_true", help="plan only (the default)")
    p.add_argument("--check", action="store_true", help="read the real Sheet, write nothing")
    p.add_argument("--live", action="store_true", help="write to the real Sheet")
    p.add_argument("--persist", action="store_true", help="store dry-run history (needs --db)")
    p.add_argument("--limit", type=int, default=25, help="companies per tab (1..500)")
    p.add_argument("--json", action="store_true", help="print the full plan as JSON")
    return p.parse_args(argv)


def sheets_problems(settings: Settings) -> list[str]:
    problems = []
    if not (settings.google_sheets_spreadsheet_id or "").strip():
        problems.append("GOOGLE_SHEETS_SPREADSHEET_ID is not set in .env")
    if not settings.google_service_account_json.is_file():
        problems.append(
            "service-account key file not found (GOOGLE_SERVICE_ACCOUNT_JSON; "
            "see docs/GOOGLE_SHEETS_SETUP_GUIDE.md)"
        )
    return problems


def preconditions(args: argparse.Namespace, settings: Settings) -> list[str]:
    problems = []
    if args.limit < 1 or args.limit > 500:
        problems.append("--limit must be 1..500")
    if args.live and args.dry_run:
        problems.append("--live and --dry-run are mutually exclusive")
    if args.live and args.check:
        problems.append("--check is read-only; drop it to write with --live")
    if args.persist and not args.db:
        problems.append("--persist requires --db")
    if args.live:
        if not args.db:
            problems.append("--live requires --db (real leads)")
        elif (settings.database_name or "").startswith(DEMO_DB_PREFIX):
            problems.append("the demo database is synthetic: it is never written to Google Sheets")
        if not settings.allow_sheets_writes:
            problems.append(
                "--live requires ALLOW_SHEETS_WRITES=true (set it for this process only)"
            )
    if args.live or args.check:
        problems += sheets_problems(settings)
    if args.db:
        state, detail = check_schema(settings)
        if state != "ok":
            problems.append(f"database schema not ready ({state}: {detail})")
    return problems


def render(report: ExportReport) -> str:
    run = report.run
    lines = [
        f"workbook: {report.spreadsheet_title or '-'} ({run.spreadsheet_ref or 'not configured'})"
        + (f" | service account: {report.service_account}" if report.service_account else ""),
    ]
    for tab in report.tabs:
        p = tab.plan
        counts = ", ".join(
            f"{a.value} {p.count(a)}" for a in type(p.rows[0].action) if p.count(a)
        ) if p.rows else "no rows"  # fmt: skip
        flags = []
        if tab.created:
            flags.append("tab will be created" if run.mode is ExportMode.DRY_RUN else "tab created")
        if p.header_write:
            flags.append("header written")
        if p.duplicates:
            flags.append(f"DUPLICATE keys in sheet: {sorted(p.duplicates)}")
        if tab.error:
            flags.append(f"ERROR {tab.error}")
        lines.append(f"  [{tab.worksheet}] {counts}" + (f"  ({'; '.join(flags)})" if flags else ""))
        for row in p.rows[:5]:
            name = row.desired.cells[2] if len(row.desired.cells) > 2 else ""
            status = row.desired.meta.get("export_status", "")
            where = f"row {row.row_number}" if row.row_number else "-"
            lines.append(
                f"      {row.action.value:9} {where:8} {row.desired.key}  {name}  [{status}]"
            )
    return "\n".join(lines)


def summary(report: ExportReport, persisted: bool) -> dict[str, Any]:
    data: dict[str, Any] = report.run.model_dump(
        mode="json",
        include={
            "id", "status", "mode", "spreadsheet_ref", "worksheets", "leads_considered",
            "inserted_count", "updated_count", "unchanged_count", "withdrawn_count",
            "skipped_count", "failed_count", "duplicate_rows_found", "api_calls",
            "error_code", "error_message",
        },
    )  # fmt: skip
    data["persisted"] = persisted
    return data


async def main_async(args: argparse.Namespace) -> int:
    settings = Settings()
    configure_logging(level="WARNING", fmt="console", secret_values=settings.secret_values())
    problems = preconditions(args, settings)
    if problems:
        print("REFUSING:\n  - " + "\n  - ".join(problems))
        return 2
    settings, fixture = output_profile(settings, fixture=not args.db)

    writer: SheetsWriter | None = None
    if args.live or args.check:
        writer = GoogleSheetsWriter(
            settings.google_sheets_spreadsheet_id or "",
            ServiceAccountTokenProvider(settings.google_service_account_json),
            timeout=settings.google_sheets_timeout_seconds,
            max_retries=settings.google_sheets_max_retries,
        )
    mode = ExportMode.LIVE if args.live else ExportMode.DRY_RUN

    store: OutputStore
    if args.db:
        engine = get_engine(settings)
        store = SqlOutputStore(engine)
        views = LeadViewService(
            SqlQualificationStore(engine), SqlEnrichmentStore(engine), SqlAnalysisStore(engine)
        )
        persist = args.live or args.persist
        source = "PostgreSQL"
    else:
        demo = await build_demo_outputs(settings)
        views = demo.views
        store, persist = demo.outputs, False
        source = "DEMO (synthetic, in-memory; never written to a Sheet)"

    target = "LIVE write" if args.live else ("read-only check" if args.check else "DRY RUN")
    print(f"mode: {target} | data: {source} | tab: {settings.google_sheets_worksheet}")
    if writer is not None:
        email = service_account_email(settings.google_service_account_json)
        print(f"service account: {email or 'unknown'} (share the Sheet with it as Editor)")
    service = SheetsExportService(
        store, views, settings, writer, mode=mode, persist=persist, fixture=fixture
    )
    report = await service.run(limit=args.limit)
    print(render(report))
    print(json.dumps(summary(report, persist), indent=2))
    if args.json:
        plan = [
            {"worksheet": t.worksheet, "key": r.desired.key, "action": r.action.value,
             "row": r.row_number, "export_status": r.desired.meta.get("export_status")}
            for t in report.tabs for r in t.plan.rows
        ]  # fmt: skip
        print(json.dumps(plan, indent=2))
    return 0 if report.run.status.value == "succeeded" else 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(main_async(args))
    except AppError as exc:
        print(f"ERROR {exc.code}: {exc.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
