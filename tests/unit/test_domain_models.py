from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.errors import InvalidStateTransitionError
from app.domain.enums import (
    EmailVerificationStatus,
    EnrichmentStatus,
    EnrichmentTarget,
    GateDecision,
    LayaDecisionKind,
    LeadStatus,
    ProviderName,
    QualificationDimension,
    ReviewStatus,
    RunStatus,
    SignalType,
    SourceType,
)
from app.domain.lifecycle import ALLOWED_TRANSITIONS, can_transition
from app.domain.models import (
    Company,
    EnrichmentResult,
    Founder,
    FundingDetails,
    LayaOutput,
    Lead,
    PipelineRun,
    Qualification,
    RuleResult,
    Signal,
    Source,
    compute_content_hash,
    normalize_domain,
)


# ---------------------------------------------------------------- normalization
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("acme.io", "acme.io"),
        ("https://www.Acme.io/about?x=1", "acme.io"),
        ("  WWW.ACME.CO.IN. ", "acme.co.in"),
        ("http://sub.acme.io:8080", "sub.acme.io"),
    ],
)
def test_normalize_domain(raw: str, expected: str) -> None:
    assert normalize_domain(raw) == expected


@pytest.mark.parametrize("bad", ["", "localhost", "not a domain", "http://", "acme"])
def test_normalize_domain_rejects_garbage(bad: str) -> None:
    with pytest.raises(ValueError, match="not a valid domain"):
        normalize_domain(bad)


def test_content_hash_is_stable_sha256() -> None:
    h = compute_content_hash("hello")
    assert h == compute_content_hash("hello")
    assert len(h) == 64


# ---------------------------------------------------------------- entities
def test_company_normalizes_domain_and_rejects_unknown_fields() -> None:
    c = Company(name="Acme", domain="https://www.acme.io", country_code="IN")
    assert c.domain == "acme.io"
    assert c.created_at.tzinfo is not None
    with pytest.raises(ValidationError):
        Company(name="Acme", unexpected_field=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Company(name="")
    with pytest.raises(ValidationError):
        Company(name="Acme", country_code="india")


def test_founder_profile_keys_lowercased() -> None:
    f = Founder(
        name="Asha", company_id=uuid4(), profile_urls={" LinkedIn ": "https://linkedin.com/in/a"}
    )
    assert list(f.profile_urls) == ["linkedin"]
    assert f.enrichment_status is None


def test_source_requires_valid_url_and_hash() -> None:
    s = Source(
        url="https://acme.io/about",
        source_type=SourceType.COMPANY_WEBSITE,
        content_hash=compute_content_hash("x"),
    )
    assert s.retrieved_at.tzinfo is not None
    with pytest.raises(ValidationError):
        Source(url="not-a-url", source_type=SourceType.NEWS)
    with pytest.raises(ValidationError):
        Source(url="https://a.io", source_type=SourceType.NEWS, content_hash="abc")
    with pytest.raises(ValidationError):  # naive datetime rejected
        Source(url="https://a.io", source_type=SourceType.NEWS, published_at=datetime(2026, 1, 1))


def test_signal_requires_evidence_source_and_bounded_confidence() -> None:
    base: dict[str, Any] = {
        "company_id": uuid4(),
        "signal_type": SignalType.HIRING,
        "summary": "Hiring",
    }
    Signal.model_validate(base | {"source_ids": [uuid4()], "confidence": 0.5})
    with pytest.raises(ValidationError):
        Signal.model_validate(base | {"source_ids": [], "confidence": 0.5})
    with pytest.raises(ValidationError):
        Signal.model_validate(base | {"source_ids": [uuid4()], "confidence": 1.5})


def test_funding_details_only_on_funding_signal_and_cap_check() -> None:
    funding = FundingDetails(amount=Decimal("28000000"), round_name="Seed")
    cap = Decimal("50000000")
    assert funding.within_cap(cap) is True
    assert FundingDetails(amount=Decimal("60000000")).within_cap(cap) is False
    assert FundingDetails(amount=None).within_cap(cap) is None
    assert FundingDetails(amount=Decimal(1), currency="USD").within_cap(cap) is None

    Signal(
        company_id=uuid4(),
        signal_type=SignalType.FUNDING,
        summary="Raised ₹2.8 Cr",
        source_ids=[uuid4()],
        confidence=0.9,
        funding=funding,
    )
    with pytest.raises(ValidationError, match="only allowed on FUNDING"):
        Signal(
            company_id=uuid4(),
            signal_type=SignalType.HIRING,
            summary="x",
            source_ids=[uuid4()],
            confidence=0.9,
            funding=funding,
        )


# ---------------------------------------------------------------- qualification
@pytest.mark.parametrize(
    ("kind", "value", "ok"),
    [
        (LayaDecisionKind.CHOICE, "fit", True),
        (LayaDecisionKind.CHOICE, 0.5, False),
        (LayaDecisionKind.SCORE, 7.5, True),
        (LayaDecisionKind.SCORE, True, False),
        (LayaDecisionKind.PROBABILITY, 0.3, True),
        (LayaDecisionKind.PROBABILITY, 1.3, False),
        (LayaDecisionKind.PROBABILITY, "high", False),
    ],
)
def test_laya_output_value_matches_kind(kind, value, ok) -> None:
    kwargs = {"dimension": QualificationDimension.AI_INTENT, "kind": kind, "value": value}
    if ok:
        LayaOutput(**kwargs)
    else:
        with pytest.raises(ValidationError):
            LayaOutput(**kwargs)


def test_cannot_enrich_when_a_hard_rule_failed() -> None:
    rules = [RuleResult(rule="funding_cap", passed=False, detail="₹8 Cr > cap")]
    q = Qualification(lead_id=uuid4(), rule_results=rules, decision=GateDecision.REJECT)
    assert q.hard_rules_passed is False
    with pytest.raises(ValidationError, match="failed a hard rule"):
        Qualification(lead_id=uuid4(), rule_results=rules, decision=GateDecision.ENRICH)


def test_review_decision_requires_review_status() -> None:
    with pytest.raises(ValidationError, match="REVIEW decision"):
        Qualification(lead_id=uuid4(), decision=GateDecision.REVIEW)
    Qualification(lead_id=uuid4(), decision=GateDecision.REVIEW, review_status=ReviewStatus.PENDING)


# ---------------------------------------------------------------- lead lifecycle
def test_every_status_has_transition_rules() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(LeadStatus)


def test_happy_path_lifecycle_with_audit_trail() -> None:
    lead = Lead(company_id=uuid4())
    lead.transition_to(LeadStatus.RESEARCHED)
    lead.transition_to(LeadStatus.QUALIFIED, reason="gate=enrich")
    lead.qualification_id = uuid4()
    lead.transition_to(LeadStatus.ENRICHMENT_PENDING)
    lead.transition_to(LeadStatus.ENRICHED)
    lead.transition_to(LeadStatus.READY)
    assert lead.status is LeadStatus.READY
    assert [c.to_status for c in lead.status_history] == [
        LeadStatus.RESEARCHED,
        LeadStatus.QUALIFIED,
        LeadStatus.ENRICHMENT_PENDING,
        LeadStatus.ENRICHED,
        LeadStatus.READY,
    ]
    assert lead.status_history[1].reason == "gate=enrich"


def test_cannot_skip_qualification_to_reach_enrichment() -> None:
    lead = Lead(company_id=uuid4())
    assert not can_transition(LeadStatus.DISCOVERED, LeadStatus.ENRICHMENT_PENDING)
    with pytest.raises(InvalidStateTransitionError):
        lead.transition_to(LeadStatus.ENRICHMENT_PENDING)
    assert lead.status is LeadStatus.DISCOVERED
    assert lead.status_history == []


def test_enrichment_requires_qualification_record() -> None:
    lead = Lead(company_id=uuid4(), status=LeadStatus.QUALIFIED)
    with pytest.raises(InvalidStateTransitionError, match="qualification record"):
        lead.transition_to(LeadStatus.ENRICHMENT_PENDING)


def test_rejected_is_terminal_except_review() -> None:
    lead = Lead(company_id=uuid4(), status=LeadStatus.REJECTED)
    with pytest.raises(InvalidStateTransitionError):
        lead.transition_to(LeadStatus.QUALIFIED)
    lead.transition_to(LeadStatus.REVIEW)


# ---------------------------------------------------------------- enrichment
def _enrichment(**kw: Any) -> EnrichmentResult:
    base: dict[str, Any] = {
        "target": EnrichmentTarget.PERSON,
        "provider": ProviderName.HUNTER,
        "status": EnrichmentStatus.SUCCESS,
    }
    return EnrichmentResult.model_validate(base | kw)


def test_enrichment_discovered_email_is_unverified_by_default() -> None:
    r = _enrichment(email="Founder@Acme.io")
    assert r.email == "founder@acme.io"
    assert r.email_verification is EmailVerificationStatus.UNVERIFIED


@pytest.mark.parametrize(
    "kw",
    [
        {"email": "not-an-email"},
        {"status": EnrichmentStatus.FAILED},  # FAILED without error
        {"email_verification": EmailVerificationStatus.VALID},  # verification without email
        {"confidence": 2.0},
        {"cost_credits": Decimal(-1)},
        {
            "attempted_at": datetime(2026, 1, 2, tzinfo=UTC),
            "completed_at": datetime(2026, 1, 1, tzinfo=UTC),
        },
    ],
)
def test_enrichment_invariants(kw) -> None:
    with pytest.raises(ValidationError):
        _enrichment(**kw)


# ---------------------------------------------------------------- pipeline run
def test_pipeline_run_lifecycle() -> None:
    run = PipelineRun()
    run.start()
    assert run.status is RunStatus.RUNNING
    with pytest.raises(InvalidStateTransitionError):
        run.start()
    with pytest.raises(InvalidStateTransitionError):
        run.finish(RunStatus.RUNNING)
    run.finish(RunStatus.SUCCEEDED)
    assert run.finished_at is not None
    assert run.started_at is not None
    assert run.finished_at >= run.started_at


def test_pipeline_run_rejects_inverted_times() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        PipelineRun(started_at=now, finished_at=now - timedelta(seconds=1))
