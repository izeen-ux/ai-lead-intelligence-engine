"""Google Sheets projection of PostgreSQL state: ONE worksheet (default "Ai Leads").

``SheetsExportService.run()``:
  1. ROWS        one row per company whose LATEST decision is QUALIFIED, or REVIEW with
                 passing hard ICP rules; the ``lead_status`` column says ``qualified`` or
                 ``needs review``. Rows already in the sheet whose company no longer belongs
                 there are marked ``withdrawn`` (never deleted). The qualification gate is
                 untouched: only QUALIFIED companies are ever enriched or analysed, so a
                 "needs review" row carries company facts and reasons, no contact.
  2. PLAN        read the tab, check/write the header, upsert by stable key (column A) -
                 insert / update / unchanged / skip (row limit); never append a key that
                 already exists, report duplicate keys found in the sheet.
  3. WRITE       one values:batchUpdate (RAW), bounded retry on 429/5xx/timeout.
  4. HISTORY     export run + one immutable item per planned row in PostgreSQL (shown in the
                 dashboard). No second or third worksheet is created.
Synthetic data (demo database, ``fixture=True``) is NEVER written to Google Sheets: a live
writer is refused. DRY RUN plans against the current sheet and writes nothing anywhere. Sheet
edits never flow back into PostgreSQL.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from app.core.config import Settings
from app.core.errors import AppError, ProviderError
from app.core.logging import get_logger
from app.domain.analysis.catalog import ServiceCatalog, default_catalog
from app.domain.enums import QualificationDecision, RunStatus
from app.domain.models import utcnow
from app.domain.outputs.leads import (
    LEADS_COLUMNS,
    LEADS_RENAMES,
    LEGACY_LEADS_COLUMNS,
    LeadView,
    lead_cells,
    row_hash,
)
from app.domain.outputs.records import ExportItemRecord, ExportMode, ExportRunRecord
from app.domain.outputs.sync import (
    DesiredRow,
    HeaderMismatchError,
    LegacyLayout,
    PlannedRow,
    RowAction,
    SheetPlan,
    plan_upsert,
    spread,
)
from app.providers.google_sheets import SheetsWriter
from app.repositories.outputs import OutputStore
from app.services.leads import LeadViewService

logger = get_logger(__name__)
CANDIDATE_CAP = 500  # hard ceiling on companies read per tab per run
FIXTURE_LABEL = "[FIXTURE - synthetic] "
LEADS_LEGACY = LegacyLayout(LEGACY_LEADS_COLUMNS, LEADS_RENAMES)
DEMO_DB_PREFIX = "lead_intel_demo"


def output_profile(settings: Settings, *, fixture: bool = False) -> tuple[Settings, bool]:
    """(settings, synthetic). ``synthetic`` is True for the ``--fixture`` option or a database
    named ``lead_intel_demo...``: such data may be planned and shown, never written to Google
    Sheets (``SheetsExportService`` refuses a live writer for it)."""
    demo_db = (settings.database_name or "").startswith(DEMO_DB_PREFIX)
    return settings, bool(fixture or demo_db)


def sheet_tabs(base: str, existing_tabs: list[str]) -> list[str]:
    """The lead tabs of a workbook in fill order: ``base``, ``base 2``, ``base 3``, ..."""
    numbered = {}
    for name in existing_tabs:
        head, _, tail = name.rpartition(" ")
        if head == base and tail.isdigit() and int(tail) >= 2:
            numbered[int(tail)] = name
    return [base, *(numbered[n] for n in sorted(numbered))]


def sheet_keys(rows: list[list[str]]) -> list[str]:
    """Lead ids (column A) of the data rows of one tab."""
    return [r[0].strip() for r in rows[1:] if r and r[0].strip().startswith("lead_")]


@dataclass
class TabResult:
    worksheet: str
    plan: SheetPlan
    created: bool = False
    error: str | None = None


@dataclass
class ExportReport:
    run: ExportRunRecord
    tabs: list[TabResult] = field(default_factory=list)
    spreadsheet_title: str | None = None
    service_account: str | None = None


class SheetsExportService:
    def __init__(
        self,
        store: OutputStore,
        views: LeadViewService,
        settings: Settings,
        writer: SheetsWriter | None,
        *,
        mode: ExportMode,
        persist: bool = True,
        catalog: ServiceCatalog | None = None,
        clock: Callable[[], datetime] = utcnow,
        fixture: bool = False,
    ) -> None:
        if mode is not ExportMode.DRY_RUN and writer is None:
            raise ValueError("a writer is required unless mode is dry_run")
        if fixture and mode is ExportMode.LIVE:
            raise ValueError("synthetic data is never written to Google Sheets")
        self._store = store
        self._views = views
        self._settings = settings
        self._writer = writer
        self._mode = mode
        self._persist = persist
        self._catalog = catalog or default_catalog()
        self._clock = clock
        self._fixture = fixture  # controlled test: label rows, never carry an email
        self._calls_at_start = 0
        self.tabs = (settings.google_sheets_worksheet,)

    # ------------------------------------------------------------------ helpers
    async def _db(self, fn: Callable[..., object], *args: object) -> None:
        if self._persist:
            await asyncio.to_thread(fn, *args)

    async def _read(
        self, worksheet: str, columns: int, existing_tabs: list[str]
    ) -> list[list[str]]:
        if self._writer is None or worksheet not in existing_tabs:
            return []
        return await self._writer.read_values(worksheet, columns)

    def _desired_leads(self, views: list[LeadView], withdrawn: list[LeadView]) -> list[DesiredRow]:
        rows = [self._lead_row(v, withdraw=False) for v in views]
        return rows + [self._lead_row(v, withdraw=True) for v in withdrawn]

    def _lead_row(self, v: LeadView, *, withdraw: bool) -> DesiredRow:
        return DesiredRow(
            key=v.lead_id,
            cells=self._label(lead_cells(v, self._catalog), LEADS_COLUMNS),
            withdraw=withdraw,
            meta={
                "company_id": v.company_id,
                "qualification_id": v.decision.id,
                "contact_id": v.contact.id if v.contact else None,
                "analysis_id": v.analysis.id if v.analysis else None,
                "export_status": v.export_status.value,
            },
        )

    def _label(self, cells: list[str], columns: tuple[str, ...]) -> list[str]:
        if not self._fixture:
            return cells
        out = list(cells)
        # every synthetic name / contact value is labelled; a synthetic address is never sent
        for name in ("company_name", "founder_name", "founder_phone", "founder_linkedin_url"):
            if name in columns and out[columns.index(name)]:
                out[columns.index(name)] = FIXTURE_LABEL + out[columns.index(name)]
        if "founder_email" in columns:
            out[columns.index("founder_email")] = ""
        return out

    async def _others(self, existing: list[list[str]], current: set[str]) -> list[LeadView]:
        """Rows in the sheet that belong to a known company but are no longer in the current
        set (e.g. no longer QUALIFIED). Unknown keys (manual notes) are never touched."""
        keys = [r[0].strip() for r in existing[1:] if r and r[0].strip()]
        stale = [k for k in dict.fromkeys(keys) if k not in current and k.startswith("lead_")]
        resolved = await asyncio.to_thread(self._store.resolve_lead_ids, stale)
        views = await asyncio.to_thread(self._views.views, list(resolved.values()))
        return [v for v in views if not v.in_sheet]

    # ------------------------------------------------------------------ run
    async def run(self, *, limit: int | None = None) -> ExportReport:
        cap = min(limit or CANDIDATE_CAP, CANDIDATE_CAP)
        run = ExportRunRecord(
            mode=self._mode,
            spreadsheet_ref=self._settings.sheets_spreadsheet_ref,
            worksheets=list(self.tabs),
            config={
                "max_rows_per_run": self._settings.export_max_rows_per_run,
                "candidate_limit": cap,
                "catalog_version": self._catalog.version,
            },
            started_at=self._clock(),
        )
        report = ExportReport(
            run=run, service_account=self._writer.identity if self._writer else None
        )
        self._calls_at_start = self._writer.api_calls if self._writer else 0
        await self._db(self._store.create_export_run, run)
        try:
            await self._export(report, cap)
        except BaseException as exc:
            cancelled = not isinstance(exc, Exception)
            run.status = RunStatus.CANCELLED if cancelled else RunStatus.FAILED
            if isinstance(exc, HeaderMismatchError):
                run.error_code = "sheet_header_mismatch"
            elif isinstance(exc, AppError):
                run.error_code = exc.code
            else:
                run.error_code = "cancelled" if cancelled else "internal_error"
            run.error_message = str(exc)[:500] or type(exc).__name__
            await self._finish(report)
            if isinstance(exc, (AppError, HeaderMismatchError)):
                logger.warning("sheets export failed", extra={"error_code": run.error_code})
                return report
            raise
        run.status = RunStatus.SUCCEEDED
        await self._finish(report)
        return report

    async def _finish(self, report: ExportReport) -> None:
        run = report.run
        run.finished_at = self._clock()
        if self._writer is not None:
            run.api_calls = self._writer.api_calls - self._calls_at_start
        items = [self._item(run, tab.worksheet, p) for tab in report.tabs for p in tab.plan.rows]
        await self._db(self._store.insert_export_items, items)
        await self._db(self._store.finish_export_run, run)

    def _item(self, run: ExportRunRecord, worksheet: str, p: PlannedRow) -> ExportItemRecord:
        meta = p.desired.meta
        return ExportItemRecord(
            run_id=run.id,
            worksheet=worksheet,
            lead_id=p.desired.key,
            company_id=meta.get("company_id"),
            qualification_id=meta.get("qualification_id"),
            contact_id=meta.get("contact_id"),
            analysis_id=meta.get("analysis_id"),
            action=p.action,
            row_number=p.row_number,
            row_hash=row_hash(p.desired.cells),
            export_status=meta.get("export_status"),
            error=meta.get("error"),
        )

    async def _export(self, report: ExportReport, cap: int) -> None:
        run = report.run
        (worksheet,) = self.tabs
        existing_tabs: list[str] = []
        if self._writer is not None:
            info = await self._writer.spreadsheet()
            report.spreadsheet_title = info.title
            existing_tabs = info.worksheets

        qualified_ids = await asyncio.to_thread(
            self._store.company_ids_by_decision, QualificationDecision.QUALIFIED, cap
        )
        review_ids = await asyncio.to_thread(
            self._store.company_ids_by_decision, QualificationDecision.REVIEW, cap
        )
        views = await asyncio.to_thread(self._views.views, [*qualified_ids, *review_ids])
        current = [v for v in views if v.in_sheet]  # qualified first, then needs review
        run.leads_considered = len(current)

        # One tab holds EXPORT_ROWS_PER_SHEET leads; then "<tab> 2", "<tab> 3", ... A lead stays
        # in the tab it was first written to, so its row is always updated in place.
        width = max(len(LEADS_COLUMNS), len(LEADS_LEGACY.header))  # see an older, wider header
        names = sheet_tabs(worksheet, existing_tabs)
        existing = {name: await self._read(name, width, existing_tabs) for name in names}
        home: dict[str, str] = {}
        for name in names:
            for key in sheet_keys(existing[name]):
                home.setdefault(key, name)
        filled = {name: len(set(sheet_keys(existing[name]))) for name in names}
        desired: dict[str, list[DesiredRow]] = {name: [] for name in names}
        capacity = self._settings.export_rows_per_sheet
        for row in self._desired_leads(current, []):
            target = home.get(row.key)
            if target is None:  # a new lead: last tab, or a fresh one when that is full
                target = names[-1]
                if filled[target] >= capacity:
                    target = f"{worksheet} {len(names) + 1}"
                    names.append(target)
                    existing[target], desired[target], filled[target] = [], [], 0
                filled[target] += 1
            desired[target].append(row)
        in_sheet = {v.lead_id for v in current}
        budget = self._settings.export_max_rows_per_run
        run.worksheets = list(names)
        for name in names:
            others = await self._others(existing[name], in_sheet)
            rows = desired[name] + [self._lead_row(v, withdraw=True) for v in others]
            if not rows and name in existing_tabs:
                continue  # an older, full tab with nothing to change
            plan = plan_upsert(
                existing[name], LEADS_COLUMNS, rows, max_writes=budget, legacy=LEADS_LEGACY
            )
            tab = TabResult(name, plan, created=name not in existing_tabs)
            report.tabs.append(tab)
            budget = max(budget - len(plan.writes), 0)
            run.duplicate_rows_found += sum(len(v) - 1 for v in plan.duplicates.values())
            try:
                await self._apply(tab, LEADS_COLUMNS, existing_tabs)
            finally:  # count what really happened (failed writes are FAIL rows)
                for p in plan.rows:
                    run.count(p.action)

    async def _apply(
        self, tab: TabResult, header: tuple[str, ...], existing_tabs: list[str]
    ) -> None:
        if self._writer is None or self._mode is ExportMode.DRY_RUN:
            return
        plan = tab.plan
        if not plan.writes and not plan.header_write and not tab.created and not plan.relayout:
            return
        if tab.created:
            await self._writer.add_worksheet(tab.worksheet)
            existing_tabs.append(tab.worksheet)
        rows: list[tuple[int, Sequence[str | None]]]
        rows = [(1, list(header))] if (plan.header_write or tab.created) else []
        rows += [(p.row_number, p.desired.cells) for p in plan.writes if p.row_number]
        rows += plan.relayout  # layout conversion of rows this run does not rewrite
        if plan.clear_width > len(header):  # narrower layout: blank the dropped columns
            rows = [(n, [*cells, *[""] * (plan.clear_width - len(cells))]) for n, cells in rows]
        if plan.positions is not None:  # team-edited layout: each value under its own header
            rows = [(n, spread([str(c) for c in cells], plan.positions)) for n, cells in rows]
        try:
            for start in range(0, len(rows), 100):  # bounded request size
                await self._writer.write_rows(tab.worksheet, rows[start : start + 100])
        except ProviderError as exc:
            tab.error = exc.code
            failed = {id(p) for p in plan.writes}
            plan.rows = [
                PlannedRow(
                    DesiredRow(p.desired.key, p.desired.cells, p.desired.withdraw,
                               {**p.desired.meta, "error": exc.code}),
                    RowAction.FAIL,
                    p.row_number,
                )
                if id(p) in failed
                else p
                for p in plan.rows
            ]  # fmt: skip
            raise
