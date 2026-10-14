"""Export service over the demo world + in-memory workbook (no Google, no DB): ONE worksheet
("Ai Leads") holding qualified leads and leads waiting for review."""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.core.config import Settings
from app.core.errors import ProviderNotConfiguredError, ProviderUnavailableError
from app.demo import CORE_DEMO_PAGES, DemoOutputs, build_demo_outputs
from app.domain.enums import RunStatus
from app.domain.outputs.leads import LEADS_COLUMNS
from app.domain.outputs.records import ExportMode, ReviewAction
from app.domain.outputs.sync import RowAction
from app.providers.google_sheets import InMemorySheetsWriter
from app.services.review import ReviewService
from app.services.sheets_export import SheetsExportService, output_profile

SID = "1SecretSpreadsheetId_Example_XYZ123"
TAB = "Ai Leads"


@pytest.fixture
async def ctx(make_settings) -> tuple[Settings, DemoOutputs]:
    settings = make_settings(google_sheets_spreadsheet_id=SID)
    return settings, await build_demo_outputs(settings, dict(CORE_DEMO_PAGES))


def export(settings: Settings, demo: DemoOutputs, sheet: InMemorySheetsWriter | None,
           mode: ExportMode = ExportMode.MOCK, **kw: Any) -> SheetsExportService:  # fmt: skip
    return SheetsExportService(demo.outputs, demo.views, settings, sheet, mode=mode, **kw)


def col(tab: list[list[str]], name: str) -> list[str]:
    i = LEADS_COLUMNS.index(name)
    return [r[i] if len(r) > i else "" for r in tab[1:]]


def statuses(tab: list[list[str]]) -> dict[str, str]:
    return dict(zip(col(tab, "company_name"), col(tab, "lead_status"), strict=True))


def by_name(demo: DemoOutputs, name: str) -> Any:
    return next(
        c for c in demo.world.company_ids if demo.world.enrichment.load_company(c).name == name
    )


# ================================================================== one worksheet
async def test_first_export_creates_exactly_one_tab(ctx, make_settings) -> None:
    settings, demo = ctx
    assert make_settings().google_sheets_worksheet == TAB
    sheet = InMemorySheetsWriter()
    report = await export(settings, demo, sheet).run()
    run = report.run
    assert run.status is RunStatus.SUCCEEDED and run.error_code is None
    assert list(sheet.tabs) == [TAB]  # no Review_Queue, no Export_Runs, no FIXTURE_ tabs
    rows = sheet.tabs[TAB]
    assert rows[0] == list(LEADS_COLUMNS) and rows[0][:2] == ["lead_id", "lead_status"]
    assert statuses(rows) == {
        "Quikcart": "qualified", "Ecoride Fleet": "qualified", "Drivnly": "needs review",
    }  # fmt: skip
    assert (run.inserted_count, run.updated_count, run.unchanged_count) == (3, 0, 0)
    assert run.leads_considered == 3 and run.spreadsheet_ref == "...XYZ123"
    assert run.worksheets == [TAB]


async def test_a_review_row_shows_why_and_carries_no_contact_or_offer(ctx) -> None:
    settings, demo = ctx
    sheet = InMemorySheetsWriter()
    await export(settings, demo, sheet).run()
    rows = sheet.tabs[TAB]
    i = col(rows, "company_name").index("Drivnly")
    row = dict(zip(LEADS_COLUMNS, rows[i + 1], strict=True))
    assert row["lead_status"] == "needs review" and "funding cap" in row["status_reasons"]
    for blank in ("founder_email", "founder_phone", "recommended_offer", "outreach_hook"):
        assert row[blank] == ""  # the gate is untouched: REVIEW is never enriched or analysed
    ready = dict(zip(LEADS_COLUMNS, rows[col(rows, "company_name").index("Quikcart") + 1],
                     strict=True))  # fmt: skip
    assert ready["lead_status"] == "qualified" and ready["founder_email"]
    assert ready["recommended_offer"] and ready["outreach_hook"]


def test_sheet_shows_only_the_clear_columns() -> None:
    assert len(LEADS_COLUMNS) == 20 and LEADS_COLUMNS[:3] == (
        "lead_id",
        "lead_status",
        "company_name",
    )
    for technical in ("company_id", "qualification_id", "contact_id", "analysis_id",
                      "field_provenance", "export_status", "observed_facts"):  # fmt: skip
        assert technical not in LEADS_COLUMNS


async def test_review_with_failing_hard_rules_stays_out_of_the_sheet(make_settings) -> None:
    settings = make_settings(google_sheets_spreadsheet_id=SID)
    cases = {**CORE_DEMO_PAGES, "Q07": "### Mira Shah - Founder & CEO"}  # REVIEW, rules fail
    demo = await build_demo_outputs(settings, cases)
    sheet = InMemorySheetsWriter()
    await export(settings, demo, sheet).run()
    assert "Brewbox" not in col(sheet.tabs[TAB], "company_name")


async def test_history_is_recorded_with_traceable_ids(ctx) -> None:
    settings, demo = ctx
    report = await export(settings, demo, InMemorySheetsWriter()).run()
    stored = demo.outputs.runs[report.run.id]
    assert stored.status is RunStatus.SUCCEEDED and stored.finished_at is not None
    items = demo.outputs.export_items(report.run.id)
    assert {(i.worksheet, i.action) for i in items} == {(TAB, RowAction.INSERT)}
    lead = next(i for i in items if i.export_status == "ready")
    assert lead.company_id and lead.qualification_id and lead.contact_id and lead.analysis_id
    assert lead.row_number in (2, 3) and lead.row_hash and len(lead.row_hash) == 64
    assert {i.export_status for i in items} == {"ready", "needs_review"}


# ================================================================== idempotency
async def test_second_run_writes_nothing_and_creates_no_duplicates(ctx) -> None:
    settings, demo = ctx
    sheet = InMemorySheetsWriter()
    await export(settings, demo, sheet).run()
    before = [list(r) for r in sheet.tabs[TAB]]
    second = (await export(settings, demo, sheet).run()).run
    assert sheet.tabs == {TAB: before}
    assert (second.inserted_count, second.updated_count, second.unchanged_count) == (0, 0, 3)
    assert len(sheet.keys(TAB)) == len(set(sheet.keys(TAB))) == 3


async def test_changed_lead_updates_its_own_row(ctx) -> None:
    settings, demo = ctx
    sheet = InMemorySheetsWriter()
    await export(settings, demo, sheet).run()
    rows_before = len(sheet.tabs[TAB])
    sheet.tabs[TAB][1][LEADS_COLUMNS.index("company_name")] = "edited in the sheet"
    run = (await export(settings, demo, sheet).run()).run
    assert run.updated_count == 1 and run.inserted_count == 0
    assert len(sheet.tabs[TAB]) == rows_before
    assert "edited in the sheet" not in json.dumps(sheet.tabs[TAB])  # DB state wins


async def test_status_follows_the_latest_decision_in_the_same_row(ctx) -> None:
    settings, demo = ctx
    sheet = InMemorySheetsWriter()
    await export(settings, demo, sheet).run()
    review = ReviewService(demo.world.qualifications, demo.outputs, settings)
    reopened = await review.act(
        by_name(demo, "Quikcart"), ReviewAction.REOPEN, reviewer="Asha", reason="re-check date"
    )
    rejected = await review.act(
        by_name(demo, "Drivnly"), ReviewAction.REJECT, reviewer="Asha", reason="not our ICP"
    )
    assert reopened.applied and rejected.applied
    run = (await export(settings, demo, sheet).run()).run
    assert run.inserted_count == 0 and len(sheet.tabs[TAB]) == 4  # same rows, none deleted
    assert statuses(sheet.tabs[TAB]) == {
        "Quikcart": "needs review", "Ecoride Fleet": "qualified", "Drivnly": "withdrawn",
    }  # fmt: skip
    assert run.withdrawn_count == 1


async def test_manual_rows_and_foreign_keys_are_left_alone(ctx) -> None:
    settings, demo = ctx
    sheet = InMemorySheetsWriter()
    await export(settings, demo, sheet).run()
    sheet.tabs[TAB].append(["my note", "please call"])
    sheet.tabs[TAB].append(["lead_ffffffffffffffff", "unknown company"])
    run = (await export(settings, demo, sheet).run()).run
    assert run.failed_count == run.inserted_count == run.updated_count == 0
    assert sheet.tabs[TAB][-2:] == [["my note", "please call"],
                                    ["lead_ffffffffffffffff", "unknown company"]]  # fmt: skip


async def test_duplicate_rows_in_sheet_are_reported(ctx) -> None:
    settings, demo = ctx
    sheet = InMemorySheetsWriter()
    await export(settings, demo, sheet).run()
    sheet.tabs[TAB].append(list(sheet.tabs[TAB][1]))  # someone copy-pasted a row
    report = await export(settings, demo, sheet).run()
    assert report.run.duplicate_rows_found == 1
    assert len(sheet.tabs[TAB]) == 5  # nothing appended for the duplicated key


# ================================================================== limits / dry run
async def test_row_write_limit(ctx) -> None:
    settings, demo = ctx
    sheet = InMemorySheetsWriter()
    limited = settings.model_copy(update={"export_max_rows_per_run": 1})
    run = (await export(limited, demo, sheet).run()).run
    assert (run.inserted_count, run.skipped_count) == (1, 2)
    assert len(sheet.keys(TAB)) == 1
    second = (await export(limited, demo, sheet).run()).run  # next run catches up
    assert (second.inserted_count, second.unchanged_count, second.skipped_count) == (1, 1, 1)


async def test_dry_run_reads_but_never_writes(ctx) -> None:
    settings, demo = ctx
    sheet = InMemorySheetsWriter()
    report = await export(settings, demo, sheet, mode=ExportMode.DRY_RUN).run()
    assert sheet.tabs == {} and report.run.mode is ExportMode.DRY_RUN
    assert report.run.inserted_count == 3  # planned
    assert [t.worksheet for t in report.tabs] == [TAB] and report.tabs[0].created
    offline = await export(settings, demo, None, mode=ExportMode.DRY_RUN).run()
    assert offline.run.status is RunStatus.SUCCEEDED and offline.run.api_calls == 0


def test_writer_required_unless_dry_run(ctx) -> None:
    settings, demo = ctx
    with pytest.raises(ValueError, match="writer"):
        export(settings, demo, None, mode=ExportMode.LIVE)


async def test_persist_false_keeps_no_history(ctx) -> None:
    settings, demo = ctx
    await export(settings, demo, InMemorySheetsWriter(), persist=False).run()
    assert demo.outputs.runs == {} and demo.outputs.items == []


# ================================================================== failures
async def test_hard_failure_header_mismatch(ctx) -> None:
    settings, demo = ctx
    sheet = InMemorySheetsWriter()
    sheet.tabs[TAB] = [["Company", "Domain"], ["Drivn", "drivn.co.in"]]  # someone else's tab
    report = await export(settings, demo, sheet).run()
    assert report.run.status is RunStatus.FAILED
    assert report.run.error_code == "sheet_header_mismatch"
    assert sheet.tabs[TAB] == [["Company", "Domain"], ["Drivn", "drivn.co.in"]]
    assert demo.outputs.runs[report.run.id].status is RunStatus.FAILED


async def test_write_failure_marks_rows_failed_and_run_failed(ctx) -> None:
    settings, demo = ctx
    sheet = InMemorySheetsWriter()

    async def failing(worksheet: str, rows: Any) -> None:
        raise ProviderUnavailableError("google_sheets", "503 after retries")

    sheet.write_rows = failing  # type: ignore[method-assign]
    report = await export(settings, demo, sheet).run()
    run = report.run
    assert run.status is RunStatus.FAILED and run.error_code == "provider_unavailable"
    assert run.failed_count == 3 and run.inserted_count == 0
    items = demo.outputs.export_items(run.id)
    assert {i.action for i in items} == {RowAction.FAIL}
    assert all(i.error == "provider_unavailable" for i in items)


async def test_access_refused_fails_cleanly(ctx) -> None:
    settings, demo = ctx
    sheet = InMemorySheetsWriter()
    sheet.fail_next = [ProviderNotConfiguredError("google_sheets", "HTTP 403")] * 3
    report = await export(settings, demo, sheet).run()
    assert report.run.status is RunStatus.FAILED
    assert report.run.error_code == "provider_not_configured"
    assert TAB not in sheet.tabs


# ================================================================== secrecy + synthetic data
async def test_spreadsheet_id_never_appears_in_history_or_sheet(ctx, caplog) -> None:
    settings, demo = ctx
    sheet = InMemorySheetsWriter()
    report = await export(settings, demo, sheet).run()
    blob = json.dumps(sheet.tabs) + report.run.model_dump_json()
    blob += json.dumps([i.model_dump(mode="json") for i in demo.outputs.items])
    assert SID not in blob and SID not in caplog.text
    assert report.run.spreadsheet_ref == "...XYZ123"


def test_synthetic_data_can_never_be_written_live(ctx, make_settings) -> None:
    settings, demo = ctx
    with pytest.raises(ValueError, match="never written to Google Sheets"):
        export(settings, demo, InMemorySheetsWriter(), mode=ExportMode.LIVE, fixture=True)
    demo_db = make_settings(
        database_url="postgresql+psycopg://u:p@localhost:5432/lead_intel",
        database_name_override="lead_intel_demo",
    )
    assert output_profile(demo_db) == (demo_db, True)
    assert output_profile(settings) == (settings, False)
    assert output_profile(settings, fixture=True)[1] is True


async def test_synthetic_plan_is_labelled_and_carries_no_email(ctx) -> None:
    settings, demo = ctx
    sheet = InMemorySheetsWriter()
    await export(settings, demo, sheet, fixture=True).run()  # MOCK mode: in-memory only
    names = col(sheet.tabs[TAB], "company_name")
    assert names and all(n.startswith("[FIXTURE - synthetic] ") for n in names)
    assert set(col(sheet.tabs[TAB], "founder_email")) == {""}


# ================================================================== the team edits the sheet
async def test_team_columns_in_the_sheet_do_not_break_the_export(ctx) -> None:
    """Regression (2026-10-12): an empty column inserted before lead_owner failed every run
    with sheet_header_mismatch. Values are now written under their own header NAME."""
    settings, demo = ctx
    sheet = InMemorySheetsWriter()
    await export(settings, demo, sheet).run()
    at = LEADS_COLUMNS.index("lead_owner")
    notes = ["call status", "called, no answer", "", "meeting on Friday"]
    for row, note in zip(sheet.tabs[TAB], notes, strict=True):  # a team column mid-sheet ...
        row[:] = [*row, *[""] * (len(LEADS_COLUMNS) - len(row))]
        row[at:at] = ["", note]  # ... after an empty spacer column
    edited = [list(r) for r in sheet.tabs[TAB]]

    same = (await export(settings, demo, sheet).run()).run
    assert same.status is RunStatus.SUCCEEDED and same.error_code is None
    assert (same.inserted_count, same.updated_count, same.unchanged_count) == (0, 0, 3)
    assert sheet.tabs[TAB] == edited  # nothing rewritten, nothing shifted

    name_at = edited[0].index("company_name")
    sheet.tabs[TAB][1][name_at] = "typo in the sheet"
    fixed = (await export(settings, demo, sheet).run()).run
    assert (fixed.updated_count, fixed.inserted_count) == (1, 0)
    assert sheet.tabs[TAB] == edited  # our cell restored; the team's notes untouched
    assert [r[at + 1] for r in sheet.tabs[TAB]] == notes


async def test_a_renamed_column_is_refused_with_a_message_that_says_which(ctx) -> None:
    settings, demo = ctx
    sheet = InMemorySheetsWriter()
    await export(settings, demo, sheet).run()
    sheet.tabs[TAB][0][LEADS_COLUMNS.index("founder_email")] = "Email"
    before = [list(r) for r in sheet.tabs[TAB]]
    run = (await export(settings, demo, sheet).run()).run
    assert run.status is RunStatus.FAILED and run.error_code == "sheet_header_mismatch"
    assert "founder_email" in (run.error_message or "") and sheet.tabs[TAB] == before
