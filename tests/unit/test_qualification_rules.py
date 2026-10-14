"""Phase 5 hard ICP rules: funding parser, boundaries, windows, identity, geography, intent."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.config import OverCapFundingPolicy
from app.domain.enums import DomainResolutionStatus, QualificationPath, RuleOutcome, SignalType
from app.domain.qualification.funding import extract_funding, format_inr
from app.domain.qualification.icp import POLICY_VERSION, IcpConfig
from tests.qualification_factories import evaluate, sig

CR = Decimal(10_000_000)


# ================================================================== funding amount parser
@pytest.mark.parametrize(
    ("text", "amount", "currency"),
    [
        ("Drivn Raises Rs 45 Crore From Avaana Capital", 45 * CR, "INR"),
        ("Startup bags ₹5 Cr seed", 5 * CR, "INR"),
        ("raises INR 4.5 crore", Decimal("4.5") * CR, "INR"),
        ("secures Rs 50 lakh from angels", Decimal(5_000_000), "INR"),
        ("raised 3 crore in pre-seed", 3 * CR, "INR"),  # Indian unit implies INR
        ("raises $2 million seed", Decimal(2_000_000), "USD"),
    ],
)
def test_extracts_round_amounts(text: str, amount: Decimal, currency: str) -> None:
    m = extract_funding(text)
    assert m is not None
    assert (m.amount, m.currency, m.cumulative) == (amount, currency, False)


@pytest.mark.parametrize(
    "text",
    [
        "Drivn raises seed funding from angel investors",  # no amount
        "Seed Shares Priced at Rs 132.40 Apiece",  # bare number, no unit
        "raises 5 million",  # no currency, not an Indian unit
        "at a valuation of Rs 200 crore",  # valuation, not the round
        "reports revenue of Rs 30 crore",  # revenue
    ],
)
def test_ignores_non_round_amounts(text: str) -> None:
    assert extract_funding(text) is None


def test_first_round_amount_wins_over_share_price() -> None:
    m = extract_funding("Drivn Raises Rs 45 Crore, Seed Shares Priced At Rs 132.40 Apiece")
    assert m is not None and m.amount == 45 * CR


def test_cumulative_totals_are_flagged() -> None:
    m = extract_funding("raises undisclosed amount, taking its total funding to Rs 12 crore so far")
    assert m is not None and m.cumulative


def test_fx_conversion_only_when_configured() -> None:
    m = extract_funding("raises $500k")
    assert m is not None
    assert m.amount_inr(None) is None
    assert m.amount_inr(Decimal(84)) == Decimal(42_000_000)


def test_format_inr() -> None:
    assert format_inr(Decimal(50_000_000)) == "₹5 Cr"
    assert format_inr(None) == "unknown"


# ================================================================== funding path (A)
@pytest.mark.parametrize(
    ("amount_text", "outcome"),
    [
        ("Rs 5 crore", RuleOutcome.PASS),  # exactly at the cap
        ("Rs 4.99 crore", RuleOutcome.PASS),  # just below
        ("Rs 5.01 crore", RuleOutcome.FAIL),  # just above
        ("Rs 45 crore", RuleOutcome.FAIL),
    ],
)
def test_funding_cap_boundaries(amount_text: str, outcome: RuleOutcome) -> None:
    hard = evaluate([sig(f"Acme Robotics raises {amount_text} seed round")])
    assert hard.funding.outcome is outcome
    assert hard.over_cap_recent is (outcome is RuleOutcome.FAIL)


@pytest.mark.parametrize(
    ("days", "outcome"),
    [
        (89, RuleOutcome.PASS),
        (90, RuleOutcome.PASS),
        (90.5, RuleOutcome.FAIL),
        (91, RuleOutcome.FAIL),
    ],
)
def test_funding_window_boundaries(days: float, outcome: RuleOutcome) -> None:
    hard = evaluate([sig("Acme Robotics raises Rs 3 crore", days_ago=days)])
    assert hard.funding.outcome is outcome


def test_missing_funding_amount_is_unknown_never_pass() -> None:
    hard = evaluate([sig("Acme Robotics raises seed funding from angels")])
    assert hard.funding.outcome is RuleOutcome.UNKNOWN
    assert "round amount missing" in hard.funding.reason
    assert hard.path is QualificationPath.NONE


def test_missing_funding_date_is_unknown() -> None:
    hard = evaluate([sig("Acme Robotics raises Rs 2 crore", days_ago=None)])
    assert hard.funding.outcome is RuleOutcome.UNKNOWN
    assert "date missing" in hard.funding.reason


def test_cumulative_amount_does_not_pass() -> None:
    hard = evaluate([sig("Acme Robotics raises funding, total funding Rs 3 crore so far")])
    assert hard.funding.outcome is RuleOutcome.UNKNOWN
    assert "cumulative" in hard.funding.reason


def test_usd_round_unknown_without_fx_and_checked_with_fx() -> None:
    signals = [sig("Acme Robotics raises $500k seed")]
    assert evaluate(signals).funding.outcome is RuleOutcome.UNKNOWN
    with_fx = IcpConfig(usd_to_inr_rate=Decimal(84))  # $500k = ₹4.2 Cr
    assert evaluate(signals, with_fx).funding.outcome is RuleOutcome.PASS


def test_name_only_funding_is_not_attributable() -> None:
    hard = evaluate([sig("Acme Robotics raises Rs 3 crore", attributed=False)])
    assert hard.funding.outcome is RuleOutcome.UNKNOWN
    assert "not attributable" in hard.funding.reason


def test_conflicting_amounts_for_one_event_are_unknown() -> None:
    hard = evaluate(
        [
            sig("Acme Robotics raises Rs 4 crore seed round", source="a"),
            sig("Acme Robotics raises Rs 40 crore seed round", source="b"),
        ]
    )
    assert hard.funding.outcome is RuleOutcome.UNKNOWN
    assert "conflicting" in hard.funding.reason


def test_small_and_large_recent_rounds_conflict() -> None:
    hard = evaluate(
        [
            sig("Acme Robotics raises Rs 3 crore angel round", days_ago=80, source="a"),
            sig("Acme Robotics closes Rs 50 crore Series A", days_ago=5, source="b"),
        ]
    )
    assert hard.funding.outcome is RuleOutcome.UNKNOWN
    assert hard.over_cap_recent


def test_no_funding_evidence_is_not_applicable() -> None:
    assert evaluate([]).funding.outcome is RuleOutcome.NOT_APPLICABLE


def test_cap_is_configuration_driven() -> None:
    signals = [sig("Acme Robotics raises Rs 8 crore")]
    assert evaluate(signals).funding.outcome is RuleOutcome.FAIL
    assert evaluate(signals, IcpConfig(funding_cap_inr=10 * CR)).funding.outcome is RuleOutcome.PASS


# ================================================================== intent path (B)
def test_strong_attributed_intent_passes() -> None:
    hard = evaluate(
        [sig("Acme Robotics deploys AI vision in 20 warehouses", SignalType.AI_INTEREST)]
    )
    assert hard.intent.outcome is RuleOutcome.PASS
    assert hard.path is QualificationPath.INTENT


@pytest.mark.parametrize(("days", "outcome"), [(90, RuleOutcome.PASS), (91, RuleOutcome.FAIL)])
def test_intent_window(days: int, outcome: RuleOutcome) -> None:
    hard = evaluate([sig("Acme Robotics expands to Pune", SignalType.EXPANSION, days_ago=days)])
    assert hard.intent.outcome is outcome


def test_intent_window_is_configurable() -> None:
    signals = [sig("Acme Robotics expands to Pune", SignalType.EXPANSION, days_ago=120)]
    assert evaluate(signals, IcpConfig(intent_window_days=180)).intent.outcome is RuleOutcome.PASS


@pytest.mark.parametrize(
    "signal",
    [
        sig("Acme Robotics is hiring an intern", SignalType.HIRING),  # hiring-only
        sig("Acme among 10 startups at meetup", SignalType.EXPANSION, attributed=False),  # namesake
        sig("Acme Robotics expands", SignalType.EXPANSION, confidence=0.5),  # low confidence
    ],
)
def test_weak_intent_fails(signal) -> None:
    hard = evaluate([signal])
    assert hard.intent.outcome is RuleOutcome.FAIL
    assert "weak" in hard.intent.reason


def test_hiring_with_growth_cue_counts() -> None:
    title = "Acme Robotics to hire 150 for new plant expansion"
    hard = evaluate(
        [sig(title, SignalType.HIRING, source="a"), sig(title, SignalType.EXPANSION, source="a")]
    )
    assert hard.intent.outcome is RuleOutcome.PASS


def test_funding_and_intent_paths_are_independent() -> None:
    hard = evaluate(
        [
            sig("Acme Robotics raises Rs 45 crore", source="a"),
            sig("Acme Robotics launches AI platform for 50 hospitals", SignalType.AI_INTEREST),
        ]
    )
    assert hard.funding.outcome is RuleOutcome.FAIL
    assert hard.intent.outcome is RuleOutcome.PASS
    assert hard.path is QualificationPath.INTENT
    assert hard.over_cap_recent
    assert hard.passed
    assert hard.qualified_allowed(OverCapFundingPolicy.IGNORE)
    assert not hard.qualified_allowed(OverCapFundingPolicy.REVIEW)


# ================================================================== identity / geography
@pytest.mark.parametrize(
    "status",
    [s for s in DomainResolutionStatus if s is not DomainResolutionStatus.VERIFIED],
)
def test_unverified_identity_fails(status: DomainResolutionStatus) -> None:
    hard = evaluate([sig("Acme Robotics raises Rs 3 crore")], domain_status=status)
    assert hard.identity.outcome is RuleOutcome.FAIL
    assert not hard.passed and not hard.qualified_allowed(OverCapFundingPolicy.IGNORE)


def test_verified_status_without_domain_fails() -> None:
    hard = evaluate([], domain=None)
    assert hard.identity.outcome is RuleOutcome.FAIL


def test_geography_explicit_country() -> None:
    assert evaluate([], country_code="IN").geography.outcome is RuleOutcome.PASS
    assert evaluate([], country_code="US").geography.outcome is RuleOutcome.FAIL


def test_geography_inferred_from_cctld_and_evidence() -> None:
    assert evaluate([], domain="acme.co.in").geography.outcome is RuleOutcome.PASS
    unknown = evaluate([], domain="acme.com")
    assert unknown.geography.outcome is RuleOutcome.UNKNOWN
    inr = evaluate([sig("Acme Robotics raises Rs 3 crore")], domain="acme.com")
    assert inr.geography.outcome is RuleOutcome.PASS
    city = evaluate(
        [sig("Acme Robotics opens Bengaluru office", SignalType.EXPANSION)], domain="acme.com"
    )
    assert city.geography.outcome is RuleOutcome.PASS
    namesake = evaluate(
        [sig("Acme opens India office", SignalType.EXPANSION, attributed=False)], domain="acme.com"
    )
    assert namesake.geography.outcome is RuleOutcome.UNKNOWN


def test_non_india_icp_configuration() -> None:
    us = IcpConfig(country_code="US", country_tlds=("us",))
    assert evaluate([], us, country_code="US").geography.outcome is RuleOutcome.PASS
    assert evaluate([], us, country_code="IN").geography.outcome is RuleOutcome.FAIL
    assert evaluate([], us, domain="acme.in").geography.outcome is RuleOutcome.UNKNOWN


def test_icp_config_from_settings_and_snapshot(make_settings) -> None:
    s = make_settings(
        funding_cap_inr=Decimal(30_000_000),
        icp_funding_window_days=60,
        icp_country_tlds="in, co.in",
        icp_over_cap_funding_policy="reject",
    )
    icp = IcpConfig.from_settings(s)
    assert icp.funding_cap_inr == 3 * CR
    assert icp.funding_window_days == 60
    assert icp.country_tlds == ("in", "co.in")
    assert icp.over_cap_funding_policy is OverCapFundingPolicy.REJECT
    snap = icp.snapshot()
    assert snap["policy_version"] == POLICY_VERSION
    assert snap["funding_cap_inr"] == "30000000"


def test_settings_reject_inverted_laya_thresholds(make_settings) -> None:
    with pytest.raises(ValueError, match="LAYA_REJECT_THRESHOLD"):
        make_settings(laya_qualify_threshold=0.4, laya_reject_threshold=0.5)
