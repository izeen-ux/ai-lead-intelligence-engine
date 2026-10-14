"""Phase 5 decision policy, QualificationService, LLM REVIEW routing, history, enrichment gate."""

from __future__ import annotations

import ast
import asyncio
import uuid
from pathlib import Path
from typing import Any, ClassVar

import pytest

from app.core.errors import EnrichmentNotAllowedError, ProviderTimeoutError
from app.domain.enums import (
    DomainResolutionStatus,
    ProviderName,
    QualificationDecision,
    QualificationPath,
    RunStatus,
    SignalType,
    StageStatus,
)
from app.domain.models import LayaOutput
from app.domain.qualification.icp import POLICY_VERSION
from app.domain.qualification.records import QualificationDecisionRecord
from app.providers.ai_router import AiRouterAdapter, MockAiRouterProvider
from app.providers.contracts import (
    LlmReviewRequest,
    LlmReviewResult,
    ProviderStatus,
    QualificationRequest,
    QualificationResult,
)
from app.providers.laya import MockLayaProvider
from app.repositories.qualification import InMemoryQualificationStore, QualificationInput
from app.services.enrichment_gate import ensure_enrichment_allowed
from app.services.qualification import QualificationService
from tests.qualification_factories import NOW, facts, sig

ROOT = Path(__file__).resolve().parents[2]
Q = QualificationDecision


# ---------------------------------------------------------------- scripted providers
class ScriptedLaya:
    name: ClassVar[ProviderName] = ProviderName.LAYA

    def __init__(self, fit: float = 0.9, attribution: float = 0.9, trigger: str = "funding",
                 level: float = 2.0, exc: Exception | None = None) -> None:  # fmt: skip
        self.fit, self.attribution, self.trigger, self.level, self.exc = (
            fit, attribution, trigger, level, exc,
        )  # fmt: skip
        self.requests: list[QualificationRequest] = []

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            name=self.name, mode="mock", configured=True, operational=True, paid=False
        )

    async def evaluate(self, request: QualificationRequest) -> QualificationResult:
        self.requests.append(request)
        if self.exc is not None:
            raise self.exc
        values = {
            "business_trigger": self.trigger,
            "attribution": self.attribution,
            "company_fit": self.fit,
            "evidence_quality": self.level,
        }
        return QualificationResult(
            provider=self.name,
            outputs=[
                LayaOutput(
                    dimension=q.dimension,
                    kind=q.kind,
                    value=values[q.dimension.value],
                    confidence=0.8,
                )
                for q in request.questions
            ],
            model_version="scripted",
        )


class ScriptedReviewer:
    name: ClassVar[ProviderName] = ProviderName.AI_ROUTER

    def __init__(self, decision: QualificationDecision, confidence: float = 0.9,
                 cite: str | None = "first", exc: Exception | None = None) -> None:  # fmt: skip
        self.decision, self.confidence, self.cite, self.exc = decision, confidence, cite, exc
        self.requests: list[LlmReviewRequest] = []

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            name=self.name, mode="mock", configured=True, operational=True, paid=False
        )

    async def review(self, request: LlmReviewRequest) -> LlmReviewResult:
        self.requests.append(request)
        if self.exc is not None:
            raise self.exc
        ids = {"first": request.allowed_evidence_ids[:1], "bogus": ["evt_invented"], None: []}
        return LlmReviewResult(
            provider=self.name,
            decision=self.decision,
            confidence=self.confidence,
            reasons=["scripted"],
            supporting_evidence_ids=ids[self.cite],
        )


# ---------------------------------------------------------------- scenario data
FUNDING_OK = [sig("Acme Robotics raises Rs 4 crore seed round")]
INTENT_OK = [sig("Acme Robotics deploys AI across 50 plants", SignalType.AI_INTEREST)]
OVER_CAP_PLUS_INTENT = [
    sig("Acme Robotics raises Rs 45 crore", source="f"),
    sig("Acme Robotics partners Tata to deploy 1,000 robots", SignalType.EXPANSION, source="e"),
]
MISSING_AMOUNT = [sig("Acme Robotics raises seed funding")]


def store_with(signals, **fact_kw: Any) -> tuple[InMemoryQualificationStore, uuid.UUID]:
    store = InMemoryQualificationStore()
    f = facts(company_id=uuid.uuid4(), **fact_kw)
    store.add(QualificationInput(f, list(signals)))
    return store, f.company_id


async def qualify(
    signals,
    make_settings,
    laya=None,
    reviewer=None,
    settings_kw: dict[str, Any] | None = None,
    **fact_kw: Any,
) -> tuple[QualificationDecisionRecord, InMemoryQualificationStore]:
    store, cid = store_with(signals, **fact_kw)
    service = QualificationService(
        store,
        laya or ScriptedLaya(),
        make_settings(**(settings_kw or {})),
        reviewer=reviewer,
        clock=lambda: NOW,
    )
    run, [record] = await service.run([cid])
    assert run.status is RunStatus.SUCCEEDED
    return record, store


# ================================================================== decision policy
async def test_funding_path_strong_laya_is_qualified(make_settings) -> None:
    record, _ = await qualify(FUNDING_OK, make_settings)
    assert record.decision is Q.QUALIFIED
    assert record.path is QualificationPath.FUNDING
    assert record.hard_rules_passed and record.laya_status is StageStatus.OK
    assert record.score == 0.9 and record.enrichment_eligible


async def test_intent_path_strong_laya_is_qualified(make_settings) -> None:
    record, _ = await qualify(INTENT_OK, make_settings, laya=ScriptedLaya(trigger="ai_automation"))
    assert (record.decision, record.path) == (Q.QUALIFIED, QualificationPath.INTENT)


@pytest.mark.parametrize(
    ("fact_kw", "reason"),
    [
        ({"domain_status": DomainResolutionStatus.REVIEW, "domain": None}, "identity"),
        ({"country_code": "US"}, "geography"),
    ],
)
async def test_blocking_hard_failures_skip_laya(make_settings, fact_kw, reason) -> None:
    laya = ScriptedLaya(fit=0.99)
    record, _ = await qualify(FUNDING_OK, make_settings, laya=laya, **fact_kw)
    assert record.decision is Q.NOT_QUALIFIED
    assert laya.requests == []  # hard rules are never handed to Laya to override
    assert record.laya_status is StageStatus.SKIPPED
    assert reason in record.reasons[0]


async def test_no_path_is_not_qualified(make_settings) -> None:
    record, _ = await qualify([sig("Acme Robotics raises Rs 45 crore")], make_settings)
    assert (record.decision, record.path) == (Q.NOT_QUALIFIED, QualificationPath.NONE)


async def test_missing_amount_is_review_not_qualified(make_settings) -> None:
    record, _ = await qualify(MISSING_AMOUNT, make_settings)
    assert record.decision is Q.REVIEW
    assert "insufficient evidence" in record.reasons[0]
    assert not record.enrichment_eligible


async def test_laya_negative_is_not_qualified(make_settings) -> None:
    record, _ = await qualify(FUNDING_OK, make_settings, laya=ScriptedLaya(fit=0.1))
    assert record.decision is Q.NOT_QUALIFIED
    assert record.hard_rules_passed  # hard pass + semantic poor fit


async def test_laya_ambiguous_qualifies_when_hard_rules_pass(make_settings) -> None:
    """Default policy: hard rules decide, Laya only vetoes (live data: always ambiguous)."""
    record, _ = await qualify(FUNDING_OK, make_settings, laya=ScriptedLaya(fit=0.6))
    assert record.decision is Q.QUALIFIED and record.hard_rules_passed
    assert any("ICP_LAYA_AMBIGUOUS_POLICY=qualify" in r for r in record.reasons)


async def test_laya_ambiguous_is_review_when_configured(make_settings) -> None:
    def cautious(**kw):
        return make_settings(icp_laya_ambiguous_policy="review", **kw)

    record, _ = await qualify(FUNDING_OK, cautious, laya=ScriptedLaya(fit=0.6))
    assert record.decision is Q.REVIEW


async def test_laya_timeout_falls_back_to_review(make_settings) -> None:
    laya = ScriptedLaya(exc=ProviderTimeoutError("laya", "slow"))
    record, _ = await qualify(FUNDING_OK, make_settings, laya=laya)
    assert record.decision is Q.REVIEW
    assert record.laya_status is StageStatus.ERROR
    assert record.laya_result == {"error_code": "provider_timeout", "error": "slow"}
    assert any("laya unavailable" in r for r in record.reasons)


@pytest.mark.parametrize(
    ("policy", "expected", "laya_called"),
    [("review", Q.REVIEW, True), ("reject", Q.NOT_QUALIFIED, False), ("ignore", Q.QUALIFIED, True)],
)
async def test_over_cap_policy_for_intent_path(
    make_settings, policy, expected, laya_called
) -> None:
    laya = ScriptedLaya(trigger="expansion")
    record, _ = await qualify(
        OVER_CAP_PLUS_INTENT,
        make_settings,
        laya=laya,
        settings_kw={"icp_over_cap_funding_policy": policy},
    )
    assert record.decision is expected
    assert record.path is QualificationPath.INTENT
    assert bool(laya.requests) is laya_called


async def test_unknown_geography_caps_at_review(make_settings) -> None:
    signals = [sig("Acme Robotics deploys AI across 50 plants", SignalType.AI_INTEREST)]
    record, _ = await qualify(signals, make_settings, domain="acmerobotics.com")
    assert record.decision is Q.REVIEW
    assert any("geography unconfirmed" in r for r in record.reasons)


async def test_duplicate_coverage_counts_once(make_settings) -> None:
    signals = [
        sig(f"Acme Robotics raises Rs 4 crore seed - outlet {i}", source=f"s{i}") for i in range(4)
    ]
    laya = ScriptedLaya()
    record, _ = await qualify(signals, make_settings, laya=laya)
    assert (record.signal_count, record.event_count) == (4, 1)
    assert len(laya.requests[0].state["events"]) == 1
    assert laya.requests[0].state["events"][0]["publishers"] == 1
    assert len(record.evidence_signal_ids) == 4  # every source signal kept as provenance


async def test_decision_carries_versions_and_evidence(make_settings) -> None:
    record, _ = await qualify(FUNDING_OK, make_settings)
    assert record.policy_version == POLICY_VERSION
    assert record.icp_config["funding_window_days"] == 90
    assert record.evidence_bundle["events"][0]["signal_ids"] == [str(FUNDING_OK[0].signal_id)]
    assert [c["rule"] for c in record.hard_rule_results] == [
        "identity",
        "geography",
        "funding",
        "intent",
    ]


# ================================================================== LLM review (REVIEW only)
async def test_llm_disabled_by_default(make_settings) -> None:
    reviewer = ScriptedReviewer(Q.QUALIFIED)
    record, _ = await qualify(
        FUNDING_OK, make_settings, laya=ScriptedLaya(fit=0.6), reviewer=reviewer,
        settings_kw={"icp_laya_ambiguous_policy": "review"},
    )  # fmt: skip
    assert record.decision is Q.REVIEW
    assert reviewer.requests == []
    assert record.llm_review_status is StageStatus.SKIPPED


async def test_llm_only_sees_review_cases(make_settings) -> None:
    reviewer = ScriptedReviewer(Q.NOT_QUALIFIED)
    on = {"llm_review_enabled": True}
    qualified, _ = await qualify(FUNDING_OK, make_settings, reviewer=reviewer, settings_kw=on)
    rejected, _ = await qualify([], make_settings, reviewer=reviewer, settings_kw=on)
    assert (qualified.decision, rejected.decision) == (Q.QUALIFIED, Q.NOT_QUALIFIED)
    assert reviewer.requests == []


async def test_llm_review_can_decide_an_allowed_review_case(make_settings) -> None:
    reviewer = ScriptedReviewer(Q.QUALIFIED, confidence=0.9)
    record, _ = await qualify(
        FUNDING_OK, make_settings, laya=ScriptedLaya(fit=0.6), reviewer=reviewer,
        settings_kw={"llm_review_enabled": True, "icp_laya_ambiguous_policy": "review"},
    )  # fmt: skip
    assert record.decision is Q.QUALIFIED
    assert record.llm_review_status is StageStatus.OK
    assert record.llm_review is not None and record.llm_review["accepted"] is True
    [req] = reviewer.requests
    assert req.qualified_allowed is True
    assert str(FUNDING_OK[0].signal_id) in req.allowed_evidence_ids


async def test_llm_cannot_override_impossible_hard_rules(make_settings) -> None:
    reviewer = ScriptedReviewer(Q.QUALIFIED, confidence=0.99)
    record, _ = await qualify(
        MISSING_AMOUNT, make_settings, reviewer=reviewer, settings_kw={"llm_review_enabled": True}
    )
    assert record.decision is Q.REVIEW
    assert reviewer.requests[0].qualified_allowed is False
    assert record.llm_review is not None and record.llm_review["accepted"] is False
    assert any("cannot upgrade" in r for r in record.reasons)


@pytest.mark.parametrize(
    ("reviewer", "why"),
    [
        (ScriptedReviewer(Q.QUALIFIED, confidence=0.5), "below confidence"),
        (ScriptedReviewer(Q.QUALIFIED, cite="bogus"), "not in the bundle"),
        (ScriptedReviewer(Q.QUALIFIED, cite=None), "missing"),
    ],
)
async def test_untrustworthy_llm_reviews_are_ignored(make_settings, reviewer, why) -> None:
    record, _ = await qualify(
        FUNDING_OK, make_settings, laya=ScriptedLaya(fit=0.6), reviewer=reviewer,
        settings_kw={"llm_review_enabled": True, "icp_laya_ambiguous_policy": "review"},
    )  # fmt: skip
    assert record.decision is Q.REVIEW
    assert any(why in r for r in record.reasons)


async def test_llm_can_downgrade_review_to_not_qualified(make_settings) -> None:
    reviewer = ScriptedReviewer(Q.NOT_QUALIFIED, confidence=0.9)
    record, _ = await qualify(
        MISSING_AMOUNT, make_settings, reviewer=reviewer, settings_kw={"llm_review_enabled": True}
    )
    assert record.decision is Q.NOT_QUALIFIED


async def test_llm_error_keeps_review(make_settings) -> None:
    reviewer = ScriptedReviewer(Q.QUALIFIED, exc=ProviderTimeoutError("ai_router", "slow"))
    record, _ = await qualify(
        MISSING_AMOUNT, make_settings, reviewer=reviewer, settings_kw={"llm_review_enabled": True}
    )
    assert (record.decision, record.llm_review_status) == (Q.REVIEW, StageStatus.ERROR)


async def test_mock_reviewer_never_decides(make_settings) -> None:
    record, _ = await qualify(
        MISSING_AMOUNT, make_settings, reviewer=MockAiRouterProvider(),
        settings_kw={"llm_review_enabled": True},
    )  # fmt: skip
    assert record.decision is Q.REVIEW
    assert record.llm_review is not None and record.llm_review["model"] == "mock-llm-0"


async def test_live_ai_router_is_a_guarded_contract(make_settings) -> None:
    from app.core.errors import ProviderCallBlockedError

    live = AiRouterAdapter(make_settings(provider_mode="live"))
    assert live.status().paid
    with pytest.raises(ProviderCallBlockedError):
        await live.review(
            LlmReviewRequest(subject_id=uuid.uuid4(), bundle={}, qualified_allowed=False)
        )


# ================================================================== history / immutability
async def test_history_is_append_only(make_settings) -> None:
    store, cid = store_with(FUNDING_OK)
    service = QualificationService(store, ScriptedLaya(), make_settings(), clock=lambda: NOW)
    await service.run([cid])
    service_later = QualificationService(
        store, ScriptedLaya(fit=0.1), make_settings(), clock=lambda: NOW.replace(hour=13)
    )
    await service_later.run([cid])
    history = store.history(cid)
    assert [d.decision for d in history] == [Q.NOT_QUALIFIED, Q.QUALIFIED]  # newest first
    assert store.latest_decision(cid) == history[0]
    assert len(store.runs) == 2
    with pytest.raises(ValueError, match="append-only"):
        store.insert_decision(history[0])


def test_qualified_record_requires_hard_rules() -> None:
    with pytest.raises(ValueError, match="hard rules"):
        QualificationDecisionRecord(
            run_id=uuid.uuid4(), company_id=uuid.uuid4(), company_name="x",
            decision=Q.QUALIFIED, path=QualificationPath.FUNDING, hard_rules_passed=False,
            policy_version=POLICY_VERSION,
        )  # fmt: skip


async def test_dry_run_persists_nothing(make_settings) -> None:
    store, cid = store_with(FUNDING_OK)
    service = QualificationService(store, ScriptedLaya(), make_settings(), persist=False)
    run, [record] = await service.run([cid])
    assert store.decisions == [] and store.runs == {}
    assert record.decision is Q.QUALIFIED and run.qualified_count == 1


async def test_run_counts_and_candidate_limit(make_settings) -> None:
    store = InMemoryQualificationStore()
    for i, signals in enumerate([FUNDING_OK, MISSING_AMOUNT, []]):
        store.add(
            QualificationInput(facts(company_id=uuid.uuid4(), name=f"Acme Robotics {i}"), signals)
        )
    service = QualificationService(store, ScriptedLaya(), make_settings(), clock=lambda: NOW)
    run, decisions = await service.run(limit=2)
    assert run.companies_evaluated == len(decisions) == 2
    run, _ = await service.run(limit=10)
    assert (run.qualified_count, run.review_count, run.not_qualified_count) == (1, 1, 1)


async def test_run_failure_and_cancellation_are_recorded(make_settings) -> None:
    store, _ = store_with(FUNDING_OK)
    service = QualificationService(store, ScriptedLaya(), make_settings())
    with pytest.raises(Exception, match="company not found"):
        await service.run([uuid.uuid4()])
    [failed] = store.runs.values()
    assert failed.status is RunStatus.FAILED and failed.error_code == "not_found"

    class Cancelling(ScriptedLaya):
        async def evaluate(self, request):
            raise asyncio.CancelledError

    store2, cid2 = store_with(FUNDING_OK)
    with pytest.raises(asyncio.CancelledError):
        await QualificationService(store2, Cancelling(), make_settings()).run([cid2])
    [cancelled] = store2.runs.values()
    assert cancelled.status is RunStatus.CANCELLED


# ================================================================== Phase 6 door
async def test_enrichment_gate_only_opens_for_latest_qualified(make_settings) -> None:
    record, store = await qualify(FUNDING_OK, make_settings)
    assert ensure_enrichment_allowed(store, record.company_id) == store.latest_decision(
        record.company_id
    )

    for signals, laya in ((MISSING_AMOUNT, ScriptedLaya()), (FUNDING_OK, ScriptedLaya(fit=0.1))):
        blocked, blocked_store = await qualify(signals, make_settings, laya=laya)
        with pytest.raises(EnrichmentNotAllowedError, match=blocked.decision.value):
            ensure_enrichment_allowed(blocked_store, blocked.company_id)

    with pytest.raises(EnrichmentNotAllowedError, match="never been qualified"):
        ensure_enrichment_allowed(InMemoryQualificationStore(), uuid.uuid4())


async def test_enrichment_gate_requires_current_policy_version(make_settings) -> None:
    record, store = await qualify(FUNDING_OK, make_settings)
    store.decisions[0] = record.model_copy(update={"policy_version": "p5-old"})
    with pytest.raises(EnrichmentNotAllowedError, match="older policy"):
        ensure_enrichment_allowed(store, record.company_id)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_no_code_path_from_phase5_into_enrichment_vendors() -> None:
    phase5 = [
        ROOT / "app/services/qualification.py",
        ROOT / "app/services/enrichment_gate.py",
        ROOT / "app/repositories/qualification.py",
        ROOT / "app/evaluation/qualification.py",
        ROOT / "scripts/run_qualification.py",
        ROOT / "scripts/evaluate_qualification.py",
        *sorted((ROOT / "app/domain/qualification").glob("*.py")),
    ]
    forbidden = (
        "hunter",
        "apollo",
        "enrichment_kit",
        "apify",
        "firecrawl",
        "crawl4ai",
        "agent_reach",
    )
    for path in phase5:
        bad = [m for m in _imports(path) if any(f in m for f in forbidden)]
        assert bad == [], f"{path.name} imports {bad}"


async def test_mock_pipeline_never_touches_enrichment_mocks(make_settings, registry) -> None:
    store, cid = store_with(FUNDING_OK)
    service = QualificationService(
        store, registry.qualification, make_settings(), reviewer=registry.llm_review
    )
    await service.run([cid])
    assert isinstance(registry.qualification, MockLayaProvider)
    for vendor in (registry.enrichment, registry.email_vendor, registry.people_vendor):
        assert vendor.calls == []


# ================================================================== AI attribution review
# Owner decision 2026-10-13: funding news that matches a company only by NAME goes to the AI
# reviewer instead of straight to the human queue. The AI answers one question (is the news
# about this company?); cap, window, geography and the classifier still decide the lead.
NAME_ONLY_OK = [sig("Acme Robotics raises Rs 4 crore seed round", attributed=False)]
NAME_ONLY_OVER_CAP = [sig("Acme Robotics raises Rs 45 crore", attributed=False)]
AI_ON = {"llm_review_enabled": True}


async def test_name_only_funding_is_review_without_the_ai_reviewer(make_settings) -> None:
    record, _ = await qualify(NAME_ONLY_OK, make_settings)
    assert record.decision is Q.REVIEW and record.llm_review_status is StageStatus.SKIPPED
    assert any("not attributable beyond a name match" in r for r in record.reasons)


async def test_ai_confirms_the_news_and_the_normal_rules_qualify(make_settings) -> None:
    reviewer = ScriptedReviewer(Q.QUALIFIED, confidence=0.9)
    record, _ = await qualify(NAME_ONLY_OK, make_settings, reviewer=reviewer, settings_kw=AI_ON)
    assert record.decision is Q.QUALIFIED and record.hard_rules_passed
    assert record.path is QualificationPath.FUNDING and record.laya_status is StageStatus.OK
    assert any("ai review (0.90) confirmed 1 funding event" in r for r in record.reasons)
    (request,) = reviewer.requests  # exactly one AI call
    assert request.task == "attribution" and request.question_event_ids
    assert record.llm_review is not None and record.llm_review["task"] == "attribution"
    assert record.llm_review["confirmed_event_ids"] == request.question_event_ids


async def test_ai_confirmation_never_beats_the_funding_cap(make_settings) -> None:
    reviewer = ScriptedReviewer(Q.QUALIFIED, confidence=0.95)
    record, _ = await qualify(
        NAME_ONLY_OVER_CAP, make_settings, reviewer=reviewer, settings_kw=AI_ON
    )
    assert record.decision is not Q.QUALIFIED and not record.hard_rules_passed


async def test_ai_says_the_news_is_about_another_company(make_settings) -> None:
    reviewer = ScriptedReviewer(Q.NOT_QUALIFIED, confidence=0.9)
    record, _ = await qualify(NAME_ONLY_OK, make_settings, reviewer=reviewer, settings_kw=AI_ON)
    assert record.decision is Q.NOT_QUALIFIED
    assert any("about a different company" in r for r in record.reasons)


@pytest.mark.parametrize(
    "reviewer",
    [
        ScriptedReviewer(Q.QUALIFIED, confidence=0.5),  # not sure enough
        ScriptedReviewer(Q.QUALIFIED, cite="bogus"),  # cites an event that does not exist
        ScriptedReviewer(Q.QUALIFIED, cite=None),  # cites nothing
        ScriptedReviewer(Q.REVIEW),  # cannot tell
        ScriptedReviewer(Q.QUALIFIED, exc=ProviderTimeoutError("openai", "timeout")),
    ],
)
async def test_an_unsure_or_failing_ai_leaves_the_case_with_a_human(
    make_settings, reviewer
) -> None:
    record, _ = await qualify(NAME_ONLY_OK, make_settings, reviewer=reviewer, settings_kw=AI_ON)
    assert record.decision is Q.REVIEW and not record.hard_rules_passed
    assert len(reviewer.requests) == 1


async def test_a_key_without_credits_is_asked_once_per_run(make_settings) -> None:
    from app.core.errors import ProviderError

    class NoCreditsError(ProviderError):
        code = "provider_quota_exhausted"

    reviewer = ScriptedReviewer(Q.QUALIFIED, exc=NoCreditsError("openai", "no credits"))
    store = InMemoryQualificationStore()
    ids = []
    for _ in range(3):
        f = facts(company_id=uuid.uuid4())
        store.add(QualificationInput(f, list(NAME_ONLY_OK)))
        ids.append(f.company_id)
    service = QualificationService(
        store, ScriptedLaya(), make_settings(**AI_ON), reviewer=reviewer, clock=lambda: NOW
    )
    _, records = await service.run(ids)
    assert [r.decision for r in records] == [Q.REVIEW] * 3
    assert [r.llm_review_status for r in records] == [StageStatus.ERROR] * 3
    assert len(reviewer.requests) == 1  # not three failing calls
