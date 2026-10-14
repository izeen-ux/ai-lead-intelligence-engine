"""Final decision policy - pure function, fixed precedence (see QUALIFICATION_POLICY.md).

1. Identity or geography FAIL                        -> NOT_QUALIFIED (blocking, final)
2. No path PASS:
     a path UNKNOWN (missing/conflicting data)       -> REVIEW
     otherwise                                       -> NOT_QUALIFIED
3. Hard rules pass (at least one path):
     geography UNKNOWN                               -> at most REVIEW
     intent-only + recent round above cap            -> REVIEW / NOT_QUALIFIED / ignored
                                                        (ICP_OVER_CAP_FUNDING_POLICY)
     Laya unavailable                                -> REVIEW
     Laya NEGATIVE                                   -> NOT_QUALIFIED
     Laya STRONG_POSITIVE                            -> QUALIFIED (if allowed by 3's caps)
     Laya AMBIGUOUS                                  -> QUALIFIED (if allowed by 3's caps) or
                                                        REVIEW (ICP_LAYA_AMBIGUOUS_POLICY)
4. REVIEW only -> optional LLM review. Accepted only when its confidence meets the threshold
   and it cites evidence ids that exist in the bundle; it may NEVER produce QUALIFIED when the
   hard gate forbids it (``qualified_allowed``).
Laya and the LLM can make an outcome MORE cautious freely; only hard-rule-permitted cases can
become QUALIFIED.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.config import LayaAmbiguousPolicy, OverCapFundingPolicy
from app.domain.enums import QualificationDecision, QualificationPath, RuleOutcome
from app.domain.qualification.bundle import LayaAssessment, LayaVerdict
from app.domain.qualification.icp import IcpConfig
from app.domain.qualification.rules import HardRuleEvaluation
from app.providers.contracts import LlmReviewResult


@dataclass
class PreliminaryDecision:
    """Outcome before the optional LLM review."""

    decision: QualificationDecision
    reasons: list[str]
    qualified_allowed: bool
    needs_laya: bool
    score: float | None = None
    confidence: float | None = None


def hard_gate(hard: HardRuleEvaluation, icp: IcpConfig) -> PreliminaryDecision | None:
    """Decisions that need no semantic judgement. ``None`` = continue to Laya."""
    blocking = hard.blocking_failure
    if blocking is not None:
        return PreliminaryDecision(
            QualificationDecision.NOT_QUALIFIED,
            [f"hard rule {blocking.rule} failed: {blocking.reason}"],
            qualified_allowed=False,
            needs_laya=False,
        )
    if hard.path is QualificationPath.NONE:
        if hard.any_path_unknown:
            unknown = [c for c in (hard.funding, hard.intent) if c.outcome is RuleOutcome.UNKNOWN]
            return PreliminaryDecision(
                QualificationDecision.REVIEW,
                [f"insufficient evidence - {c.rule}: {c.reason}" for c in unknown],
                qualified_allowed=False,
                needs_laya=False,
            )
        return PreliminaryDecision(
            QualificationDecision.NOT_QUALIFIED,
            [
                f"no qualification path - funding: {hard.funding.reason}; "
                f"intent: {hard.intent.reason}"
            ],
            qualified_allowed=False,
            needs_laya=False,
        )
    if (
        hard.path is QualificationPath.INTENT
        and hard.over_cap_recent
        and icp.over_cap_funding_policy is OverCapFundingPolicy.REJECT
    ):
        return PreliminaryDecision(
            QualificationDecision.NOT_QUALIFIED,
            [f"recent round above cap, ICP_OVER_CAP_FUNDING_POLICY=reject: {hard.funding.reason}"],
            qualified_allowed=False,
            needs_laya=False,
        )
    return None


def _caps(hard: HardRuleEvaluation, icp: IcpConfig) -> list[str]:
    caps = []
    if hard.geography.outcome is RuleOutcome.UNKNOWN:
        caps.append(f"geography unconfirmed: {hard.geography.reason}")
    if (
        hard.path is QualificationPath.INTENT
        and hard.over_cap_recent
        and icp.over_cap_funding_policy is OverCapFundingPolicy.REVIEW
    ):
        caps.append(
            "qualifies via intent but a recent round exceeds the funding cap "
            f"({hard.funding.reason}) - human decision (ICP_OVER_CAP_FUNDING_POLICY=review)"
        )
    return caps


def decide(
    hard: HardRuleEvaluation, laya: LayaAssessment | None, icp: IcpConfig, *, laya_error: str | None
) -> PreliminaryDecision:
    gated = hard_gate(hard, icp)
    if gated is not None:
        return gated
    reasons = [
        f"path {hard.path.value}: "
        + "; ".join(c.reason for c in (hard.funding, hard.intent) if c.outcome is RuleOutcome.PASS)
    ]
    caps = _caps(hard, icp)
    allowed = hard.qualified_allowed(icp.over_cap_funding_policy) and not caps
    if laya is None:
        return PreliminaryDecision(
            QualificationDecision.REVIEW,
            [*reasons, *caps, f"laya unavailable ({laya_error or 'no result'}) - human decision"],
            qualified_allowed=allowed,
            needs_laya=True,
        )
    reasons += list(laya.reasons)
    score, conf = laya.fit, laya.confidence
    if laya.verdict is LayaVerdict.NEGATIVE:
        return PreliminaryDecision(
            QualificationDecision.NOT_QUALIFIED, reasons + caps, allowed, True, score, conf
        )
    if laya.verdict is LayaVerdict.STRONG_POSITIVE and allowed:
        return PreliminaryDecision(
            QualificationDecision.QUALIFIED, reasons, allowed, True, score, conf
        )
    if (
        laya.verdict is LayaVerdict.AMBIGUOUS
        and allowed
        and icp.laya_ambiguous_policy is LayaAmbiguousPolicy.QUALIFY
    ):
        note = "hard ICP rules pass and laya does not reject (ICP_LAYA_AMBIGUOUS_POLICY=qualify)"
        return PreliminaryDecision(
            QualificationDecision.QUALIFIED, [*reasons, note], allowed, True, score, conf
        )
    return PreliminaryDecision(
        QualificationDecision.REVIEW, reasons + caps, allowed, True, score, conf
    )


@dataclass
class AttributionOutcome:
    """What an AI attribution check allows: events it confirmed as being about this company,
    or a final NOT_QUALIFIED when the evidence is about another company."""

    confirmed: list[str] = field(default_factory=list)  # event (cluster) ids
    decision: QualificationDecision | None = None  # only ever NOT_QUALIFIED
    reasons: list[str] = field(default_factory=list)


def apply_attribution_review(
    review: LlmReviewResult, candidate_event_ids: set[str], icp: IcpConfig
) -> AttributionOutcome:
    """The AI answers ONE question - is this funding news about this company? It never
    decides the lead: confirmed events go back through the normal hard rules (cap, window,
    geography) and the classifier. Low confidence or invented ids change nothing."""
    if review.confidence < icp.llm_review_min_confidence:
        return AttributionOutcome(
            reasons=[f"ai review below confidence threshold ({review.confidence:.2f})"]
        )
    why = "; ".join(review.reasons)[:300]
    if review.decision is QualificationDecision.NOT_QUALIFIED:
        return AttributionOutcome(
            decision=QualificationDecision.NOT_QUALIFIED,
            reasons=[
                f"ai review ({review.confidence:.2f}): the funding news is about a different "
                f"company - {why}"
            ],
        )
    confirmed = sorted(set(review.supporting_evidence_ids) & candidate_event_ids)
    if review.decision is not QualificationDecision.QUALIFIED or not confirmed:
        return AttributionOutcome(reasons=["ai review could not confirm the funding news"])
    return AttributionOutcome(
        confirmed=confirmed,
        reasons=[
            f"ai review ({review.confidence:.2f}) confirmed {len(confirmed)} funding event(s) "
            f"are about this company - {why}"
        ],
    )


@dataclass
class ReviewOutcome:
    decision: QualificationDecision
    accepted: bool
    reasons: list[str] = field(default_factory=list)


def apply_llm_review(
    preliminary: PreliminaryDecision,
    review: LlmReviewResult,
    icp: IcpConfig,
    allowed_evidence_ids: set[str],
) -> ReviewOutcome:
    """Only ever called for REVIEW. Never produces QUALIFIED when the hard gate forbids it."""
    if preliminary.decision is not QualificationDecision.REVIEW:  # defensive
        return ReviewOutcome(preliminary.decision, False, ["llm review ignored: not a REVIEW case"])
    if review.confidence < icp.llm_review_min_confidence:
        return ReviewOutcome(
            QualificationDecision.REVIEW,
            False,
            [f"llm review below confidence threshold ({review.confidence:.2f})"],
        )
    cited = set(review.supporting_evidence_ids)
    if not cited or not cited <= allowed_evidence_ids:
        return ReviewOutcome(
            QualificationDecision.REVIEW,
            False,
            ["llm review rejected: cited evidence ids missing or not in the bundle"],
        )
    if review.decision is QualificationDecision.QUALIFIED and not preliminary.qualified_allowed:
        return ReviewOutcome(
            QualificationDecision.REVIEW,
            False,
            ["llm review cannot upgrade to QUALIFIED: hard rules do not permit it"],
        )
    return ReviewOutcome(
        review.decision,
        True,
        [f"llm review ({review.confidence:.2f}): {r}" for r in review.reasons],
    )
