"""Phase 8: human REVIEW workflow - new immutable decisions through domain rules, audit trail,
hard ICP rules re-checked on approval, downstream gates unchanged, no provider calls."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.errors import EnrichmentNotAllowedError, NotFoundError
from app.demo import CORE_DEMO_PAGES, DemoOutputs, build_demo_outputs
from app.domain.enums import ContactStatus, QualificationDecision
from app.domain.outputs.records import ReviewAction, ReviewOutcome
from app.domain.qualification.icp import POLICY_VERSION, IcpConfig
from app.providers.enrichment_kit.mock import MockEnrichmentKitProvider
from app.providers.hunter.mock import MockHunterProvider
from app.services.enrichment import EnrichmentService
from app.services.enrichment_gate import ensure_enrichment_allowed
from app.services.review import ReviewService

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
async def ctx(make_settings) -> tuple[Settings, DemoOutputs]:
    settings = make_settings()
    cases = {
        **CORE_DEMO_PAGES,
        "Q07": "### Mira Shah - Founder & CEO",
    }  # Brewbox: REVIEW, rules fail
    return settings, await build_demo_outputs(settings, cases)


def cid(demo: DemoOutputs, name: str) -> UUID:
    return next(
        c for c in demo.world.company_ids if demo.world.enrichment.load_company(c).name == name
    )


def service(settings: Settings, demo: DemoOutputs) -> ReviewService:
    return ReviewService(demo.world.qualifications, demo.outputs, settings)


async def act(settings: Settings, demo: DemoOutputs, name: str, action: ReviewAction,
              **kw: Any) -> Any:  # fmt: skip
    kw.setdefault("reviewer", "Asha Rao")
    kw.setdefault("reason", "checked the evidence manually")
    return await service(settings, demo).act(cid(demo, name), action, **kw)


# ================================================================== approve
async def test_approve_creates_a_new_qualified_decision(ctx) -> None:
    settings, demo = ctx
    q = demo.world.qualifications
    company = cid(demo, "Drivnly")
    before = q.history(company)
    old = before[0].model_copy(deep=True)
    assert old.decision is QualificationDecision.REVIEW
    result = await act(settings, demo, "Drivnly", ReviewAction.APPROVE,
                       expected_decision_id=old.id)  # fmt: skip
    assert result.applied and result.decision is not None
    new = q.latest_decision(company)
    assert new is not None and new.id == result.decision.id != old.id
    assert new.decision is QualificationDecision.QUALIFIED and new.hard_rules_passed
    assert new.policy_version == POLICY_VERSION
    assert new.evidence_bundle["human_review"]["reviewer"] == "Asha Rao"
    assert new.reasons[0] == "human approve by Asha Rao: checked the evidence manually"
    history = q.history(company)
    assert len(history) == len(before) + 1
    assert next(d for d in history if d.id == old.id) == old  # the REVIEW decision is untouched
    run = q.runs[new.run_id]
    assert (run.laya_mode, run.llm_review_mode, run.qualified_count) == ("human", "human", 1)


async def test_approval_opens_downstream_gates_normally(ctx) -> None:
    settings, demo = ctx
    company = cid(demo, "Drivnly")
    icp = IcpConfig.from_settings(settings)
    with pytest.raises(EnrichmentNotAllowedError):
        ensure_enrichment_allowed(demo.world.qualifications, company, icp)
    await act(settings, demo, "Drivnly", ReviewAction.APPROVE)
    decision = ensure_enrichment_allowed(demo.world.qualifications, company, icp)
    # Phase 6 runs through its own service + gate (mocks here); the review service never does.
    await EnrichmentService(
        demo.world.enrichment, demo.world.qualifications, MockEnrichmentKitProvider(settings),
        MockHunterProvider(settings), settings,
    ).run([company])  # fmt: skip
    contacts = [
        c for c in demo.world.enrichment.contacts(company) if c.qualification_id == decision.id
    ]
    assert contacts and contacts[0].status is ContactStatus.VERIFIED


async def test_approve_is_refused_when_hard_rules_fail(ctx) -> None:
    settings, demo = ctx
    company = cid(demo, "Brewbox")
    before = demo.world.qualifications.history(company)
    assert before[0].decision is QualificationDecision.REVIEW and not before[0].hard_rules_passed
    result = await act(settings, demo, "Brewbox", ReviewAction.APPROVE)
    assert not result.applied and result.decision is None
    assert result.audit.refusal_code == "hard_rules_failed"
    assert result.audit.hard_rules_passed is False and result.audit.hard_rule_results
    assert demo.world.qualifications.history(company) == before  # nothing written
    assert demo.outputs.human_reviews(company)[0].outcome is ReviewOutcome.REFUSED


# ================================================================== reject / reopen
async def test_reject_creates_not_qualified(ctx) -> None:
    settings, demo = ctx
    result = await act(settings, demo, "Brewbox", ReviewAction.REJECT, reason="not a fit")
    assert result.applied and result.decision is not None
    assert result.decision.decision is QualificationDecision.NOT_QUALIFIED
    assert result.audit.resulting_decision_id == result.decision.id


async def test_reopen_revokes_eligibility(ctx) -> None:
    settings, demo = ctx
    company = cid(demo, "Quikcart")
    icp = IcpConfig.from_settings(settings)
    ensure_enrichment_allowed(demo.world.qualifications, company, icp)  # QUALIFIED today
    result = await act(settings, demo, "Quikcart", ReviewAction.REOPEN, reason="funding unclear")
    assert result.applied and result.decision is not None
    assert result.decision.decision is QualificationDecision.REVIEW
    with pytest.raises(EnrichmentNotAllowedError):
        ensure_enrichment_allowed(demo.world.qualifications, company, icp)


@pytest.mark.parametrize(
    ("name", "action", "code"),
    [
        ("Drivnly", ReviewAction.REOPEN, "not_allowed_from_review"),
        ("Quikcart", ReviewAction.APPROVE, "not_allowed_from_qualified"),
        ("Quikcart", ReviewAction.REJECT, "not_allowed_from_qualified"),
    ],
)
async def test_actions_only_from_allowed_states(ctx, name, action, code) -> None:
    settings, demo = ctx
    company = cid(demo, name)
    before = demo.world.qualifications.history(company)
    result = await act(settings, demo, name, action)
    assert not result.applied and result.audit.refusal_code == code
    assert demo.world.qualifications.history(company) == before


async def test_stale_view_is_refused(ctx) -> None:
    settings, demo = ctx
    result = await act(settings, demo, "Drivnly", ReviewAction.APPROVE,
                       expected_decision_id=uuid4())  # fmt: skip
    assert not result.applied and result.audit.refusal_code == "stale_decision"


async def test_double_approval_is_refused_the_second_time(ctx) -> None:
    settings, demo = ctx
    first = await act(settings, demo, "Drivnly", ReviewAction.APPROVE)
    second = await act(settings, demo, "Drivnly", ReviewAction.APPROVE)
    assert first.applied and not second.applied
    assert second.audit.refusal_code == "not_allowed_from_qualified"


# ================================================================== audit
async def test_audit_trail_records_reviewer_reason_time(ctx) -> None:
    settings, demo = ctx
    company = cid(demo, "Drivnly")
    await act(settings, demo, "Drivnly", ReviewAction.APPROVE, reviewer="Asha", reason="ok go")
    await act(settings, demo, "Drivnly", ReviewAction.REOPEN, reviewer="Ravi", reason="recheck")
    audits = demo.outputs.human_reviews(company)
    assert [(a.action, a.reviewer, a.reason) for a in audits] == [
        (ReviewAction.REOPEN, "Ravi", "recheck"), (ReviewAction.APPROVE, "Asha", "ok go"),
    ]  # fmt: skip
    assert audits[1].resulting_decision_id == audits[0].prior_decision_id
    assert all(a.reviewed_at.tzinfo is not None for a in audits)
    with pytest.raises(ValueError, match="append-only"):
        demo.outputs.insert_human_review(audits[0])


@pytest.mark.parametrize(("reviewer", "reason"), [("   ", "valid reason"), ("Asha", "no")])
async def test_reviewer_and_reason_are_required(ctx, reviewer, reason) -> None:
    settings, demo = ctx
    company = cid(demo, "Drivnly")
    before = demo.world.qualifications.history(company)
    with pytest.raises(ValidationError):
        await act(settings, demo, "Drivnly", ReviewAction.APPROVE, reviewer=reviewer, reason=reason)
    assert demo.world.qualifications.history(company) == before


async def test_unknown_company(ctx) -> None:
    settings, demo = ctx
    with pytest.raises(NotFoundError):
        await service(settings, demo).act(uuid4(), ReviewAction.APPROVE, reviewer="a", reason="abc")


# ================================================================== boundaries
OUTPUT_MODULES = {
    "app/services/review.py": set(),
    "app/services/dashboard.py": {"app.providers.contracts"},
    "app/services/leads.py": set(),
    "app/services/sheets_export.py": {"app.providers.google_sheets"},
    "app/api/routes/dashboard.py": {"app.providers.registry"},
}


@pytest.mark.parametrize(("path", "allowed"), list(OUTPUT_MODULES.items()))
def test_output_layer_never_imports_lead_data_providers(path: str, allowed: set[str]) -> None:
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    providers = {m for m in imported if m.startswith("app.providers")}
    assert providers <= allowed, providers - allowed
    assert not {m for m in imported if m.startswith(("httpx", "requests", "google"))}
