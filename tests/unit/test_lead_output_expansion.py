"""Final lead-output expansion: the redesigned ``Leads`` projection, field provenance,
in-place conversion of the old Sheet layout, the >=5-lead synthetic demo, multi-lead export
(no first-record-only behaviour), fixture-only labelling and the hard gate."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from app.core.config import Settings
from app.demo import DEMO_PAGES, DemoOutputs, build_demo_outputs, demo_pipeline
from app.domain.analysis.catalog import default_catalog
from app.domain.enums import ContactStatus, PhoneStatus, QualificationDecision, RunStatus
from app.domain.outputs.leads import (
    LEAD_FIELDS,
    LEADS_COLUMNS,
    LEADS_RENAMES,
    LEGACY_LEADS_COLUMNS,
    lead_cells,
)
from app.domain.outputs.profile import (
    NOT_FOUND,
    build_profile,
    classify_industry,
    company_linkedin,
    funding_of,
)
from app.domain.outputs.records import ExportMode
from app.domain.outputs.sync import (
    DesiredRow,
    HeaderMismatchError,
    LegacyLayout,
    RowAction,
    plan_upsert,
    relayout,
)
from app.domain.pipeline import PipelineStatus, Stage, Trigger
from app.providers.google_sheets import InMemorySheetsWriter
from app.providers.registry import mock_reasoning
from app.repositories.analysis import InMemoryAnalysisStore
from app.services.dashboard import DashboardBackend, DashboardService
from app.services.lead_analysis import LeadAnalysisService
from app.services.sheets_export import FIXTURE_LABEL, SheetsExportService, output_profile

REQUIRED = (
    "lead_id", "company_name", "website", "company_location", "industry", "founder_name",
    "founder_title", "founder_email", "email_status", "founder_phone", "phone_status",
    "founder_linkedin_url", "company_linkedin_url", "funding_amount", "funding_round",
    "funding_date", "qualification_decision", "qualification_path", "qualification_confidence",
    "primary_trigger", "verified_contact", "ai_opportunity", "recommended_offer",
    "offer_match_reason", "outreach_hook", "strongest_signal_date", "evidence_sources",
    "qualification_id", "contact_id", "analysis_id", "last_updated_at", "export_status",
)  # fmt: skip
LEGACY = LegacyLayout(LEGACY_LEADS_COLUMNS, LEADS_RENAMES)
SID = "1SecretSpreadsheetId_Example_XYZ123"
TAB = "Ai Leads"


@pytest.fixture
async def demo(make_settings) -> DemoOutputs:
    return await build_demo_outputs(make_settings())


def rows(demo: DemoOutputs) -> dict[str, dict[str, str]]:
    out = {}
    for cid in demo.world.company_ids:
        view = demo.views.view(cid)
        assert view is not None
        cells = lead_cells(view, default_catalog(), LEAD_FIELDS)  # every field
        out[view.company_name] = dict(zip(LEAD_FIELDS, cells, strict=True))
    return out


def exporter(settings: Settings, demo: DemoOutputs, sheet: InMemorySheetsWriter, **kw: Any):
    return SheetsExportService(
        demo.outputs, demo.views, settings, sheet, mode=ExportMode.MOCK, **kw
    )


def col(tab: list[list[str]], name: str) -> list[str]:
    i = LEADS_COLUMNS.index(name)
    return [r[i] if len(r) > i else "" for r in tab[1:]]


# ================================================================== projection
def test_every_requested_field_is_still_projected_and_the_sheet_is_a_subset() -> None:
    assert set(REQUIRED) <= set(LEAD_FIELDS)  # nothing was lost from the data model
    assert LEADS_COLUMNS[:2] == ("lead_id", "lead_status")  # key, then the status column
    assert set(LEADS_COLUMNS) <= set(LEAD_FIELDS) and len(LEADS_COLUMNS) == 20
    assert LEAD_FIELDS[:-1] == LEGACY_LEADS_COLUMNS and LEADS_RENAMES == {}
    assert len(set(LEAD_FIELDS)) == len(LEAD_FIELDS)


async def test_demo_has_at_least_five_distinct_ready_leads(demo) -> None:
    table = rows(demo)
    ready = {n: r for n, r in table.items() if r["export_status"] == "ready"}
    assert len(ready) >= 5
    assert len({r["lead_id"] for r in table.values()}) == len(table) == len(DEMO_PAGES)
    for name, row in ready.items():
        assert row["qualification_decision"] == "qualified" and row["verified_contact"] == "yes"
        assert row["founder_name"] and row["founder_title"] and row["founder_email"], name
        assert row["website"] and row["company_location"] == "India"
        assert row["recommended_offer"] and row["outreach_hook"] and row["analysis_id"]
        assert "synthetic-fixture-" in row["founder_linkedin_url"]
        assert "synthetic-fixture-" in row["company_linkedin_url"]
    assert len({r["website"] for r in ready.values()}) == len(ready)
    assert len({r["founder_email"] for r in ready.values()}) == len(ready)


async def test_gate_outcomes_are_unchanged_by_the_bigger_demo(demo) -> None:
    table = rows(demo)
    assert table["Drivnly"]["qualification_decision"] == "review"  # Rs 45 Cr round
    assert table["Tiffinly"]["qualification_decision"] == "not_qualified"  # Rs 5.2 Cr > cap
    for name in ("Drivnly", "Tiffinly"):
        row = table[name]
        assert row["founder_name"] == row["founder_email"] == row["contact_id"] == ""
        assert row["recommended_offer"] == row["outreach_hook"] == row["analysis_id"] == ""
    decisions = {
        cid: demo.world.qualifications.latest_decision(cid) for cid in demo.world.company_ids
    }
    for cid, decision in decisions.items():
        contacts = demo.world.enrichment.contacts(cid)
        assert decision is not None
        if decision.decision is not QualificationDecision.QUALIFIED:
            assert all(c.status is ContactStatus.BLOCKED for c in contacts)
            assert demo.analyses.analyses(cid) == [] or all(
                a.status.value == "blocked" for a in demo.analyses.analyses(cid)
            )


async def test_funding_columns_come_from_the_decision_evidence(demo) -> None:
    table = rows(demo)
    kheti = table["Kheti Labs"]
    assert (kheti["funding_amount"], kheti["funding_round"]) == ("₹4 Cr", "seed")
    assert kheti["funding_date"] and "funding event evt_" in kheti["field_provenance"]
    ledgerly = table["Ledgerly"]  # intent path: no funding event -> blank, not invented
    assert ledgerly["funding_amount"] == ledgerly["funding_round"] == ledgerly["funding_date"] == ""
    assert f"funding: {NOT_FOUND}" in ledgerly["field_provenance"]


async def test_missing_phone_and_contact_fields_stay_blank_with_a_status(demo) -> None:
    table = rows(demo)
    ecoride = table["Ecoride Fleet"]  # Apollo answered: verified email, no phone available
    assert ecoride["founder_email"] and ecoride["founder_phone"] == ""
    assert ecoride["phone_status"] == PhoneStatus.NOT_FOUND.value
    assert "founder_phone: not_found" in ecoride["field_provenance"]
    freight = table["Freightmind"]  # no provider found this person at all
    assert freight["export_status"] == "awaiting_contact" and freight["verified_contact"] == "no"
    assert freight["founder_email"] == freight["founder_phone"] == ""
    assert freight["founder_linkedin_url"] == "" and freight["analysis_id"] == ""
    assert f"founder_email: {NOT_FOUND}" in freight["field_provenance"]


async def test_field_provenance_names_the_source_of_every_contact_field(demo) -> None:
    kheti = rows(demo)["Kheti Labs"]
    lines = dict(line.split(": ", 1) for line in kheti["field_provenance"].splitlines())
    assert lines["founder_name"] == "https://khetilabs.in/about"
    assert lines["founder_email"].startswith("hunter (confidence ")
    assert lines["founder_phone"] == "hunter"
    assert lines["founder_linkedin_url"] == "hunter"
    assert lines["company_location"].startswith("qualification geography rule")
    assert lines["industry"] == "keyword 'farmers' on the company's homepage"
    assert lines["company_linkedin_url"] == "link on the company website"


async def test_default_settings_do_not_request_a_phone(make_settings) -> None:
    """The offline demo switches the phone capability on; plain settings keep it off."""
    assert make_settings().enrichment_phone_enabled is False
    assert make_settings().enrichment_max_phone_lookups_per_run == 5


# ================================================================== profile (pure)
def test_profile_prefers_stored_values_then_derives_then_stays_empty() -> None:
    stored = build_profile(location="Pune", industry="Robotics", description="a saas platform")
    assert (stored.location, stored.industry) == ("Pune", "Robotics")
    assert stored.provenance["industry"] == "company record"
    passed = [{"rule": "geography", "outcome": "pass", "reason": "inferred IN from ccTLD"}]
    derived = build_profile(description="Commercial EV leasing partner", hard_rules=passed)
    assert (derived.location, derived.industry) == ("India", "Electric mobility")
    unknown = [{"rule": "geography", "outcome": "unknown", "reason": "no evidence"}]
    empty = build_profile(description="We help teams", hard_rules=unknown)
    assert (empty.location, empty.industry, empty.company_linkedin_url) == ("", "", "")
    assert set(empty.provenance.values()) == {NOT_FOUND}


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("Dry cleaning and steam ironing with doorstep delivery", "Consumer services"),
        ("AI invoicing for SMEs", "Fintech"),
        ("We love our customers", None),
        ("Develop your team", None),  # 'ev' inside a word is not a match
    ],
)
def test_industry_needs_a_whole_word_match(text: str, label: str | None) -> None:
    hit = classify_industry(text)
    assert (hit[0] if hit else None) == label


@pytest.mark.parametrize(
    ("links", "expected"),
    [
        (
            {
                "linkedin.com": "https://www.linkedin.com/company/drivn-transition/posts/?feedView=all"
            },
            "https://www.linkedin.com/company/drivn-transition",
        ),
        (
            {"linkedin.com": "https://in.linkedin.com/in/some-founder"},
            "",
        ),  # a person, not a company
        ({"linkedin.com": "https://evil.example/company/x"}, ""),
        ({"wa.me": "https://wa.me/910000000000"}, ""),
        ({}, ""),
    ],
)
def test_company_linkedin_only_accepts_company_pages(links: dict[str, str], expected: str) -> None:
    assert company_linkedin(links) == expected


def test_funding_ignores_unattributed_and_non_funding_events() -> None:
    events = [
        {"types": ["expansion"], "date": "2026-09-27", "headline": "Acme expands"},
        {"types": ["funding"], "attributed": False, "round_amount": "₹9 Cr", "date": "2026-09-01"},
        {"types": ["funding"], "attributed": True, "round_amount": "₹3 Cr", "date": "2026-08-01",
         "headline": "Acme bags pre-seed round", "event_id": "evt_1",
         "sources": ["https://news.example/a"]},
    ]  # fmt: skip
    found = funding_of(events)
    assert (found["amount"], found["round"], found["date"]) == ("₹3 Cr", "pre-seed", "2026-08-01")
    assert funding_of(events[:2])["source"] == NOT_FOUND
    assert funding_of(events[:1], "seed") == {
        "amount": "", "round": "", "date": "", "source": NOT_FOUND
    }  # fmt: skip


# ================================================================== multi-lead export
async def test_export_writes_one_row_per_company_and_reruns_are_idempotent(demo) -> None:
    settings = Settings(_env_file=None, app_env="test", google_sheets_spreadsheet_id=SID)
    sheet = InMemorySheetsWriter()
    first = await exporter(settings, demo, sheet).run()
    assert list(sheet.tabs) == [TAB]  # one worksheet only
    leads = sheet.tabs[TAB]
    assert first.run.status is RunStatus.SUCCEEDED and leads[0] == list(LEADS_COLUMNS)
    assert len(leads) - 1 == 7 == len(set(col(leads, "lead_id")))  # 6 qualified + 1 review
    assert len(set(col(leads, "company_name"))) == 7
    assert col(leads, "lead_status").count("qualified") == 6
    assert col(leads, "lead_status").count("needs review") == 1
    assert sum(bool(v) for v in col(leads, "outreach_hook")) == 5  # five complete leads
    snapshot = [list(r) for r in leads]
    second = await exporter(settings, demo, sheet).run()
    assert (second.run.inserted_count, second.run.updated_count) == (0, 0)
    assert second.run.unchanged_count == 7 and sheet.tabs[TAB] == snapshot


async def test_synthetic_data_is_labelled_and_never_written_live(demo, make_settings) -> None:
    settings, synthetic = output_profile(
        make_settings(
            google_sheets_spreadsheet_id=SID,
            database_url="postgresql+psycopg://u:p@localhost:5432/lead_intel",
            database_name_override="lead_intel_demo",
        )
    )
    assert synthetic is True
    with pytest.raises(ValueError, match="never written to Google Sheets"):
        SheetsExportService(
            demo.outputs, demo.views, settings, InMemorySheetsWriter(),
            mode=ExportMode.LIVE, fixture=synthetic,
        )  # fmt: skip
    sheet = InMemorySheetsWriter()  # in-memory MOCK plan only
    await exporter(settings, demo, sheet, fixture=synthetic).run()
    assert set(sheet.tabs) == {TAB}
    leads = sheet.tabs[TAB]
    for name in ("company_name", "founder_name", "founder_linkedin_url"):
        assert all(v.startswith(FIXTURE_LABEL) for v in col(leads, name) if v), name
    phones = [v for v in col(leads, "founder_phone") if v]
    assert phones and all(v.startswith(f"{FIXTURE_LABEL}+91 00000 ") for v in phones)
    assert set(col(leads, "founder_email")) == {""}  # a synthetic address is never exported


async def test_dashboard_labels_every_synthetic_lead(demo, make_settings) -> None:
    backend = DashboardBackend(
        demo.outputs, demo.views, demo.world.qualifications, "demo", synthetic=True
    )
    service = DashboardService(backend, list, make_settings())
    leads = await service.leads(QualificationDecision.QUALIFIED, 25, 0)
    assert len(leads) == 6 and len({lead["company_id"] for lead in leads}) == 6
    assert all(lead["company_name"].startswith(FIXTURE_LABEL) for lead in leads)
    kheti = next(lead for lead in leads if lead["company_name"].endswith("Kheti Labs"))
    detail = await service.lead_detail(UUID(kheti["company_id"]))
    contact, company = detail["contact"], detail["company"]
    for key in ("name", "email", "phone", "linkedin_url"):
        assert contact[key].startswith(FIXTURE_LABEL), key
    assert contact["phone_status"] == "found"
    assert (company["location"], company["industry"]) == ("India", "Agritech")
    assert company["funding"]["amount"] == "₹4 Cr"
    assert company["field_provenance"]["founder_phone"] == "hunter"
    real = DashboardService(
        DashboardBackend(demo.outputs, demo.views, demo.world.qualifications, "postgresql"),
        list,
        make_settings(),
    )
    assert not any(FIXTURE_LABEL in lead["company_name"] for lead in await real.leads(None, 25, 0))


# ================================================================== old Sheet layout
def legacy_row(key: str, name: str) -> list[str]:
    values = {c: f"{c}-{key}" for c in LEGACY_LEADS_COLUMNS}
    values.update(lead_id=key, company_name=name)
    return [values[c] for c in LEGACY_LEADS_COLUMNS]


def test_relayout_keeps_values_by_name_and_drops_removed_columns() -> None:
    cells = relayout(legacy_row("lead_a", "Acme"), LEGACY, LEADS_COLUMNS)
    new = dict(zip(LEADS_COLUMNS, cells, strict=True))
    assert (new["lead_id"], new["company_name"]) == ("lead_a", "Acme")
    assert new["website"] == "website-lead_a" and new["founder_email"] == "founder_email-lead_a"
    assert len(cells) == len(LEADS_COLUMNS) < len(LEGACY_LEADS_COLUMNS)
    assert "company_id-lead_a" not in cells  # a dropped technical column


def test_old_layout_is_converted_in_place_without_losing_rows() -> None:
    existing = [
        list(LEGACY_LEADS_COLUMNS),
        legacy_row("lead_a", "Acme"),
        legacy_row("lead_gone", "Orphan"),  # not exported any more: converted, not dropped
        ["operator note"],
    ]
    fresh = ["lead_a", "Acme v2"] + [""] * (len(LEADS_COLUMNS) - 2)
    added = ["lead_b", "Beta"] + [""] * (len(LEADS_COLUMNS) - 2)
    plan = plan_upsert(
        existing,
        LEADS_COLUMNS,
        [DesiredRow("lead_a", fresh), DesiredRow("lead_b", added)],
        max_writes=50,
        legacy=LEGACY,
    )
    assert plan.header_write is True
    assert [(p.desired.key, p.action, p.row_number) for p in plan.rows] == [
        ("lead_a", RowAction.UPDATE, 2),
        ("lead_b", RowAction.INSERT, 5),
    ]
    moved = dict(plan.relayout)
    assert set(moved) == {3, 4}
    assert moved[3][LEADS_COLUMNS.index("website")] == "website-lead_gone"
    assert plan.clear_width == len(LEGACY_LEADS_COLUMNS)  # dropped columns get blanked
    assert moved[4][0] == "operator note"


def test_conversion_also_moves_rows_the_write_budget_skipped() -> None:
    existing = [list(LEGACY_LEADS_COLUMNS), legacy_row("lead_a", "Acme"), legacy_row("lead_b", "B")]
    width = len(LEADS_COLUMNS)
    desired = [DesiredRow(k, [k] + ["x"] * (width - 1)) for k in ("lead_a", "lead_b")]
    plan = plan_upsert(existing, LEADS_COLUMNS, desired, max_writes=1, legacy=LEGACY)
    assert [p.action for p in plan.rows] == [RowAction.UPDATE, RowAction.SKIP]
    assert [number for number, _ in plan.relayout] == [3]  # never left in the old positions


def test_a_foreign_header_is_still_refused() -> None:
    with pytest.raises(HeaderMismatchError):
        plan_upsert([["name", "phone"]], LEADS_COLUMNS, [], max_writes=5, legacy=LEGACY)
    current = plan_upsert([list(LEADS_COLUMNS)], LEADS_COLUMNS, [], max_writes=5, legacy=LEGACY)
    assert current.header_write is False and current.relayout == []


async def test_exporter_converts_a_sheet_written_with_the_old_header(demo) -> None:
    settings = Settings(_env_file=None, app_env="test", google_sheets_spreadsheet_id=SID)
    sheet = InMemorySheetsWriter()
    await exporter(settings, demo, sheet).run()
    lead_ids = col(sheet.tabs[TAB], "lead_id")
    sheet.tabs[TAB] = [list(LEGACY_LEADS_COLUMNS)] + [
        legacy_row(key, f"old {key}") for key in lead_ids
    ]
    report = await exporter(settings, demo, sheet).run()
    leads = sheet.tabs[TAB]
    width = len(LEADS_COLUMNS)
    assert report.run.status is RunStatus.SUCCEEDED and leads[0][:width] == list(LEADS_COLUMNS)
    assert col(leads, "lead_id") == lead_ids  # same rows, same order, no duplicates
    assert not any(name.startswith("old ") for name in col(leads, "company_name"))
    # the 20 dropped columns are blanked in the header and in every row - nothing stale is left
    assert all(set(row[width:]) <= {""} for row in leads)
    again = await exporter(settings, demo, sheet).run()
    assert again.run.unchanged_count == 7 and again.run.updated_count == 0


# ================================================================== pipeline over many leads
async def test_pipeline_processes_every_company_independently(make_settings) -> None:
    """Nothing is pre-analysed: one pipeline run must analyse all five eligible leads."""
    settings = make_settings()
    demo = await build_demo_outputs(settings)
    demo.analyses.__dict__.update(InMemoryAnalysisStore().__dict__)  # forget the analyses
    pipeline = demo_pipeline(settings, demo)
    run = await pipeline.run(
        trigger=Trigger.CLI, dry_run=False, limit=10, stages=[Stage.ANALYSIS, Stage.EXPORT]
    )
    assert run.status is PipelineStatus.SUCCEEDED and run.company_limit == 10
    analysis = run.result(Stage.ANALYSIS)
    assert analysis is not None and analysis.counts["succeeded"] == 5
    analysed = {a.company_id for cid in demo.world.company_ids for a in demo.analyses.analyses(cid)}
    assert len(analysed) == 5
    export = run.result(Stage.EXPORT)
    assert export is not None and export.counts["leads"] == 7  # 6 qualified + 1 review


async def test_analysis_runs_for_several_leads_with_distinct_results(make_settings) -> None:
    settings = make_settings(analysis_max_per_run=10)
    demo = await build_demo_outputs(settings)
    store = InMemoryAnalysisStore()
    run, records = await LeadAnalysisService(
        store, demo.world.qualifications, demo.world.enrichment, mock_reasoning(settings), settings
    ).run(demo.world.company_ids, limit=10)
    done = [r for r in records if r.status.value == "succeeded"]
    assert run.succeeded_count == len(done) == 5
    assert len({r.company_id for r in done}) == 5 == len({r.contact_id for r in done})
