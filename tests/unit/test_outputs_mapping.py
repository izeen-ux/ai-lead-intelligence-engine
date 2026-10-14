"""Phase 8: lead view -> Sheet row mapping and the pure idempotent upsert planner."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.core.config import Settings
from app.demo import CORE_DEMO_PAGES, DemoOutputs, build_demo_outputs
from app.domain.analysis.catalog import default_catalog
from app.domain.enums import ContactStatus, QualificationDecision
from app.domain.outputs.leads import (
    CELL_LIMIT,
    LEAD_FIELDS,
    ExportStatus,
    LeadView,
    clean,
    lead_cells,
    lead_id_for,
    row_hash,
)
from app.domain.outputs.sync import (
    DesiredRow,
    HeaderMismatchError,
    RowAction,
    check_header,
    pad,
    plan_upsert,
)
from app.services.leads import pick_contact


@pytest.fixture
async def demo(make_settings) -> DemoOutputs:
    return await build_demo_outputs(make_settings(), dict(CORE_DEMO_PAGES))


def view_named(demo: DemoOutputs, name: str) -> LeadView:
    for cid in demo.world.company_ids:
        v = demo.views.view(cid)
        if v is not None and v.company_name == name:
            return v
    raise AssertionError(name)


def cells(view: LeadView) -> dict[str, str]:
    values = lead_cells(view, default_catalog(), LEAD_FIELDS)  # every field, not only the sheet
    return dict(zip(LEAD_FIELDS, values, strict=True))


# ================================================================== lead id + view
def test_lead_id_is_stable_and_derived_from_the_company() -> None:
    cid = uuid.UUID("fb8ea7b6-3b0b-5ebe-a5ce-9fcd70f3d012")
    assert lead_id_for(cid) == "lead_fb8ea7b63b0b5ebe" == lead_id_for(uuid.UUID(str(cid)))


async def test_ready_lead_row_is_traceable(demo) -> None:
    v = view_named(demo, "Ecoride Fleet")
    row = cells(v)
    assert v.export_status is ExportStatus.READY
    assert row["lead_id"] == v.lead_id and row["company_id"] == str(v.company_id)
    assert row["qualification_id"] == str(v.decision.id)
    assert v.contact is not None and row["contact_id"] == str(v.contact.id)
    assert v.analysis is not None and row["analysis_id"] == str(v.analysis.id)
    assert row["founder_email"] == v.contact.email and row["email_status"] == "valid"
    assert row["qualification_decision"] == "qualified" and row["export_status"] == "ready"
    assert row["recommended_offer"].endswith(f"({v.analysis.recommended_offer_id})")
    assert "[evt_" in row["observed_facts"] or "[qual_" in row["observed_facts"]  # cited ids
    assert row["evidence_sources"].startswith("http")
    assert row["evidence_count"] == str(v.decision.event_count)
    assert row["last_updated_at"].endswith("+00:00")


async def test_unverified_email_is_never_exported(demo) -> None:
    v = view_named(demo, "Quikcart")
    assert v.contact is not None
    unverified = v.contact.model_copy(update={"status": ContactStatus.UNVERIFIED})
    other = LeadView(v.company_id, v.company_name, v.domain, v.domain_status, v.decision,
                     unverified, None, None)  # fmt: skip
    row = cells(other)
    assert row["founder_email"] == "" and row["founder_name"] == v.contact.full_name
    assert row["export_status"] == ExportStatus.AWAITING_CONTACT.value


async def test_status_progression(demo) -> None:
    v = view_named(demo, "Quikcart")
    no_contact = LeadView(v.company_id, v.company_name, v.domain, v.domain_status, v.decision)
    assert no_contact.export_status is ExportStatus.AWAITING_CONTACT
    no_analysis = LeadView(v.company_id, v.company_name, v.domain, v.domain_status, v.decision,
                           v.contact)  # fmt: skip
    assert no_analysis.export_status is ExportStatus.AWAITING_ANALYSIS
    review = v.decision.model_copy(update={"decision": QualificationDecision.REVIEW})
    gone = LeadView(v.company_id, v.company_name, v.domain, v.domain_status, review, v.contact,
                    v.analysis)  # fmt: skip
    assert gone.export_status is ExportStatus.NEEDS_REVIEW  # reopened, hard rules still pass
    row = cells(gone)
    assert row["outreach_hook"] == row["founder_email"] == row["recommended_offer"] == ""
    assert row["company_name"] == "Quikcart"  # identity kept, outreach material removed
    rejected = v.decision.model_copy(update={"decision": QualificationDecision.NOT_QUALIFIED})
    out = LeadView(v.company_id, v.company_name, v.domain, v.domain_status, rejected, v.contact,
                   v.analysis)  # fmt: skip
    assert out.export_status is ExportStatus.WITHDRAWN and not out.in_sheet
    assert cells(out)["founder_email"] == cells(out)["outreach_hook"] == ""


def test_review_row_is_a_lead_row_with_status_and_reasons(demo) -> None:
    v = view_named(demo, "Drivnly")
    row = cells(v)
    assert v.in_sheet and (row["lead_status"], row["export_status"]) == (
        "needs review",
        "needs_review",
    )
    assert row["qualification_decision"] == "review" and row["status_reasons"]
    assert row["founder_email"] == row["recommended_offer"] == row["analysis_id"] == ""


async def test_pick_contact_prefers_verified_under_the_same_decision(demo) -> None:
    v = view_named(demo, "Ecoride Fleet")
    assert v.contact is not None
    contacts = demo.world.enrichment.contacts(v.company_id)
    assert pick_contact(contacts, v.decision) == v.contact
    other = v.decision.model_copy(update={"id": uuid.uuid4()})
    assert pick_contact(contacts, other) is None  # contacts of an older decision don't count


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, ""),
        ("  a\x00b\x07c  ", "a b c"),
        ('=HYPERLINK("x")', '=HYPERLINK("x")'),  # kept as text; written RAW, never evaluated
        (3, "3"),
    ],
)
def test_clean(raw: Any, expected: str) -> None:
    assert clean(raw) == expected


def test_clean_bounds_length() -> None:
    out = clean("x" * (CELL_LIMIT + 50))
    assert len(out) == CELL_LIMIT and out.endswith("…")


def test_row_hash_is_order_sensitive() -> None:
    assert row_hash(["a", "b"]) != row_hash(["b", "a"]) and len(row_hash(["a"])) == 64


# ================================================================== planner
H = ("key", "name", "value")


def rows(*items: tuple[str, str, str]) -> list[DesiredRow]:
    return [DesiredRow(k, [k, n, v]) for k, n, v in items]


def test_empty_sheet_inserts_after_header() -> None:
    plan = plan_upsert([], H, rows(("a", "A", "1"), ("b", "B", "2")), max_writes=10)
    assert plan.header_write
    assert [(r.action, r.row_number) for r in plan.rows] == [
        (RowAction.INSERT, 2), (RowAction.INSERT, 3),
    ]  # fmt: skip


def test_existing_rows_update_or_stay_unchanged() -> None:
    existing = [list(H), ["a", "A", "1"], ["b", "B"]]  # trailing empty cell omitted by Sheets
    plan = plan_upsert(existing, H, rows(("a", "A", "1"), ("b", "B", ""), ("c", "C", "3")),
                       max_writes=10)  # fmt: skip
    assert not plan.header_write
    assert [(r.desired.key, r.action, r.row_number) for r in plan.rows] == [
        ("a", RowAction.UNCHANGED, 2), ("b", RowAction.UNCHANGED, 3), ("c", RowAction.INSERT, 4),
    ]  # fmt: skip
    changed = plan_upsert(existing, H, rows(("a", "A", "9")), max_writes=10)
    assert [(r.action, r.row_number) for r in changed.rows] == [(RowAction.UPDATE, 2)]


def test_withdraw_is_an_update_of_the_existing_row() -> None:
    existing = [list(H), ["a", "A", "1"]]
    plan = plan_upsert(existing, H, [DesiredRow("a", ["a", "A", "gone"], withdraw=True)],
                       max_writes=10)  # fmt: skip
    assert [(r.action, r.row_number) for r in plan.rows] == [(RowAction.WITHDRAW, 2)]


def test_duplicates_in_sheet_are_reported_and_never_multiplied() -> None:
    existing = [list(H), ["a", "A", "1"], ["x", "note"], ["a", "A", "old"]]
    plan = plan_upsert(existing, H, rows(("a", "A", "2")), max_writes=10)
    assert plan.duplicates == {"a": [2, 4]}
    assert [(r.action, r.row_number) for r in plan.rows] == [(RowAction.UPDATE, 2)]


def test_desired_key_twice_is_planned_once() -> None:
    plan = plan_upsert([], H, rows(("a", "A", "1"), ("a", "A", "2")), max_writes=10)
    assert len(plan.rows) == 1


def test_write_limit_turns_extra_writes_into_skips() -> None:
    existing = [list(H), ["a", "A", "1"]]
    plan = plan_upsert(existing, H, rows(("a", "A", "1"), ("b", "B", "2"), ("c", "C", "3")),
                       max_writes=1)  # fmt: skip
    assert [r.action for r in plan.rows] == [RowAction.UNCHANGED, RowAction.INSERT, RowAction.SKIP]
    assert plan.count(RowAction.SKIP) == 1 and len(plan.writes) == 1


def test_blank_rows_are_not_reused_and_unknown_rows_untouched() -> None:
    existing = [list(H), ["manual", "note"], [], ["a", "A", "1"]]
    plan = plan_upsert(existing, H, rows(("b", "B", "2")), max_writes=10)
    assert [(r.action, r.row_number) for r in plan.rows] == [(RowAction.INSERT, 5)]


@pytest.mark.parametrize(
    ("row1", "write"),
    [([], True), (["key", "name"], True), (["key", "name", "value"], False),
     (["key", "name", "value", ""], False)],
)  # fmt: skip
def test_header_rules(row1: list[str], write: bool) -> None:
    assert check_header([row1] if row1 else [], H) is write


@pytest.mark.parametrize("row1", [["Company", "Domain"], ["key", "other"], ["name", "key"]])
def test_foreign_header_is_refused(row1: list[str]) -> None:
    with pytest.raises(HeaderMismatchError):
        plan_upsert([row1, ["x"]], H, rows(("a", "A", "1")), max_writes=10)


def test_pad() -> None:
    assert pad(["a"], 3) == ["a", "", ""] and pad(["a", "b", "c", "d"], 2) == ["a", "b"]


def test_settings_mask_the_spreadsheet_id() -> None:
    s = Settings(_env_file=None, google_sheets_spreadsheet_id="1AbCdEfGhIjKlMnOp")
    assert s.sheets_spreadsheet_ref == "...KlMnOp"
    assert "1AbCdEfGhIjKlMnOp" in s.secret_values()  # redacted from logs
    assert Settings(_env_file=None).sheets_spreadsheet_ref is None
