"""Pre-screen: a free scrape / skip verdict from discovery data, before any website credit."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.domain.enums import LayaDecisionKind, QualificationDimension
from app.domain.models import LayaOutput
from app.domain.pipeline import STAGE_ORDER, Stage
from app.domain.prescreen import (
    DiscoveryFacts,
    PrescreenDecision,
    decide,
    laya_fit,
    laya_request,
    rule_reasons,
)
from app.domain.qualification.icp import IcpConfig
from app.providers.contracts import QualificationResult
from app.providers.laya import MockLayaProvider
from app.services.pipeline import parse_stages
from app.utils.funding_headlines import plausible_company_name

ICP = IcpConfig()


def facts(name: str = "Acme Pay", title: str = "Acme Pay raises Rs 3 Cr in seed round",
          **kw: str) -> DiscoveryFacts:  # fmt: skip
    return DiscoveryFacts(company_id=uuid.uuid4(), name=name, title=title, **kw)


def test_prescreen_runs_after_discovery_and_before_the_paid_website_stage(make_settings) -> None:
    order = list(STAGE_ORDER)
    assert order.index(Stage.DISCOVERY) < order.index(Stage.PRESCREEN) < order.index(Stage.WEBSITE)
    default = parse_stages(make_settings().pipeline_stages)
    assert default[0] is Stage.PRESCREEN and Stage.WEBSITE in default
    assert make_settings().prescreen_required is True


def test_a_small_round_headline_gets_the_green_signal() -> None:
    outcome = decide(facts(), ICP, fit=0.62)
    assert outcome.decision is PrescreenDecision.SCRAPE
    assert outcome.reasons == ("rules passed; laya P(prospect)=0.62",)


@pytest.mark.parametrize(
    ("name", "title", "why"),
    [
        ("TimbuckDo", "TimbuckDo raises Rs 120 Crore to expand", "cap rule"),
        ("Factrika", "Factrika has raised ₹8.9 crore in seed funding", "cap rule"),
        ("Voltra", "Voltra raises Series C round to scale", "stage rule"),
        ("Zeta", "Zeta turns unicorn after new round", "stage rule"),
        ("Looking to", "Looking to launch your startup in India?", "name rule"),
        ("Ecosystem", "Ecosystem expands as Indian startups grow", "name rule"),
        ("Factrika has", "Factrika has raised money", "name rule"),
    ],
)
def test_rules_skip_candidates_before_any_credit_is_spent(name: str, title: str, why: str) -> None:
    outcome = decide(facts(name, title), ICP, fit=0.9)  # even a keen Laya cannot override
    assert outcome.decision is PrescreenDecision.SKIP
    assert any(r.startswith(why) for r in outcome.reasons)


def test_cap_rule_uses_the_same_cap_and_respects_the_ignore_policy() -> None:
    over = facts("Gridlink", "Gridlink raises Rs 45 crore from Avaana Capital")
    assert rule_reasons(over, ICP)
    assert not rule_reasons(over, IcpConfig(over_cap_funding_policy="ignore"))
    assert not rule_reasons(facts("Kheti Labs", "Kheti Labs raises Rs 5 crore seed round"), ICP)
    usd = facts("Dextr AI", "Dextr AI raises $6.7M seed round")
    assert not rule_reasons(usd, ICP)  # no FX rate configured: unknown, not skipped
    assert rule_reasons(usd, IcpConfig(usd_to_inr_rate=Decimal(83)))
    total = facts("Skillnest", "Skillnest raises funds, taking total funding to Rs 40 crore")
    assert not rule_reasons(total, ICP)  # lifetime totals are not a round


def test_laya_can_only_veto() -> None:
    assert decide(facts(), ICP, fit=0.25).decision is PrescreenDecision.SKIP
    assert decide(facts(), ICP, fit=0.31).decision is PrescreenDecision.SCRAPE
    assert decide(facts(), ICP, fit=None).decision is PrescreenDecision.SCRAPE  # Laya off


async def test_laya_request_is_one_question_about_discovery_data_only() -> None:
    f = facts(description="Seed round led by angels", source="funding_news")
    request = laya_request(f)
    assert [q.dimension for q in request.questions] == [QualificationDimension.COMPANY_FIT]
    assert request.state["events"][0]["headline"] == f.title
    result = await MockLayaProvider().evaluate(request)
    fit = laya_fit(result)
    assert fit is not None and decide(f, ICP, fit).decision is PrescreenDecision.SCRAPE


def test_laya_fit_ignores_non_numeric_answers() -> None:
    answer = LayaOutput(
        dimension=QualificationDimension.COMPANY_FIT, kind=LayaDecisionKind.CHOICE,
        value="funding", confidence=0.5,
    )  # fmt: skip
    result = QualificationResult(provider=MockLayaProvider().name, outputs=[answer])
    assert laya_fit(result) is None


@pytest.mark.parametrize(
    ("name", "ok"),
    [("Acme Pay", True), ("UpKraft", True), ("MeitY Just", False), ("startups in india", False)],
)
def test_plausible_company_name(name: str, ok: bool) -> None:
    assert plausible_company_name(name) is ok


@pytest.mark.parametrize(
    ("name", "title"),
    [
        ("Peak XV", "Peak XV Raises Surge Seed Investment Ceiling to $5 Million, Unveils Cohort"),
        ("Blume Ventures", "Blume Ventures raises Rs 4 crore for its seed programme"),
        ("Acme Capital", "Acme Capital closes its new fund at Rs 3 crore"),
    ],
)
def test_investors_and_funds_are_not_prospects(name: str, title: str) -> None:
    """Regression (live run): the VC firm Peak XV went through as a funded startup."""
    outcome = decide(facts(name, title), ICP, fit=0.9)
    assert outcome.decision is PrescreenDecision.SKIP
    assert any(r.startswith("investor rule") for r in outcome.reasons)
