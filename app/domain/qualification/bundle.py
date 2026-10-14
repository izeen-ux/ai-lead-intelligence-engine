"""Evidence bundle (what Laya / the LLM see) + the typed Laya questions + their interpretation.

The bundle is built from EVENT CLUSTERS, not raw signals, so four publishers covering one round
appear once (with ``publishers: 4``). The persisted record keeps every signal id per event;
the Laya state omits ids (noise for the model) and stays well inside the checkpoint context.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from app.domain.enums import LayaDecisionKind, QualificationDimension
from app.domain.qualification.clustering import EventCluster
from app.domain.qualification.funding import format_inr
from app.domain.qualification.icp import IcpConfig
from app.domain.qualification.rules import CompanyFacts, HardRuleEvaluation
from app.providers.contracts import LayaQuestion, QualificationResult

MAX_EVENTS = 8
_HEADLINE_CHARS = 200


def _event(c: EventCluster, icp: IcpConfig, *, with_ids: bool) -> dict[str, Any]:
    amounts = [m.amount_inr(icp.usd_to_inr_rate) for m in c.funding if not m.cumulative]
    data: dict[str, Any] = {
        "date": c.event_date.date().isoformat() if c.event_date else None,
        "types": sorted(t.value for t in c.types),
        "headline": c.headline[:_HEADLINE_CHARS],
        "publishers": len(c.publishers),
        "attributed": c.attributed,
    }
    known = [a for a in amounts if a is not None]
    if known:
        data["round_amount"] = format_inr(max(known))
    if with_ids:
        data["event_id"] = c.cluster_id
        data["signal_ids"] = [str(i) for i in c.signal_ids]
        data["sources"] = sorted({s.url for s in c.signals})
        data["confidence"] = c.confidence
    return data


@dataclass(frozen=True)
class EvidenceBundle:
    facts: CompanyFacts
    hard: HardRuleEvaluation
    events: list[EventCluster]  # in-window clusters, newest first, at most MAX_EVENTS
    total_signals: int
    total_events: int
    icp: IcpConfig
    built_at: datetime

    @property
    def signal_ids(self) -> list[str]:
        return [str(i) for c in self.events for i in c.signal_ids]

    @property
    def event_ids(self) -> list[str]:
        return [c.cluster_id for c in self.events]

    def for_laya(self) -> dict[str, Any]:
        return {
            "company": self.facts.name,
            "domain": self.facts.domain,
            "target_profile": (
                f"startups in {self.icp.country_code}; recent funding round <= "
                f"{format_inr(self.icp.funding_cap_inr)} or recent AI/automation, expansion, "
                "customer-deployment or growth activity"
            ),
            "events": [_event(c, self.icp, with_ids=False) for c in self.events],
        }

    def as_record(self) -> dict[str, Any]:
        return {
            "company": self.facts.name,
            "domain": self.facts.domain,
            "domain_status": self.facts.domain_status.value,
            "built_at": self.built_at.isoformat(),
            "total_signals": self.total_signals,
            "total_events": self.total_events,
            "hard_rules": [c.as_dict() for c in self.hard.checks],
            "events": [_event(c, self.icp, with_ids=True) for c in self.events],
        }


def build_bundle(
    facts: CompanyFacts,
    hard: HardRuleEvaluation,
    clusters: list[EventCluster],
    icp: IcpConfig,
    now: datetime,
) -> EvidenceBundle:
    window = max(icp.funding_window_days, icp.intent_window_days)
    recent = [
        c for c in clusters if c.event_date is not None and (now - c.event_date).days <= window
    ]
    qualifying = {c.cluster_id for c in hard.qualifying_clusters}
    # qualifying events first, then the rest; each newest first (clusters are pre-sorted)
    ordered = [c for c in recent if c.cluster_id in qualifying] + [
        c for c in recent if c.cluster_id not in qualifying
    ]
    return EvidenceBundle(
        facts=facts,
        hard=hard,
        events=ordered[:MAX_EVENTS],
        total_signals=sum(len(c.signals) for c in clusters),
        total_events=len(clusters),
        icp=icp,
        built_at=now,
    )


# ------------------------------------------------------------------ Laya questions
class Trigger(StrEnum):
    FUNDING = "funding"
    AI_AUTOMATION = "ai_automation"
    EXPANSION = "expansion"
    CUSTOMER_DEPLOYMENT = "customer_deployment"
    HIRING_GROWTH = "hiring_growth"
    NONE = "none"


LAYA_QUESTIONS: tuple[LayaQuestion, ...] = (
    LayaQuestion(
        dimension=QualificationDimension.BUSINESS_TRIGGER,
        kind=LayaDecisionKind.CHOICE,
        instructions="What is the main recent business trigger shown by these events?",
        criteria={
            Trigger.FUNDING: "the company recently raised a funding round",
            Trigger.AI_AUTOMATION: "the company is adopting AI, automation or digital "
            "transformation",
            Trigger.EXPANSION: "the company is expanding or scaling: new markets, capacity, "
            "fleet, offices",
            Trigger.CUSTOMER_DEPLOYMENT: "the company won customers, partners or large deployments",
            Trigger.HIRING_GROWTH: "the company is hiring to support growth",
            Trigger.NONE: "no meaningful recent trigger: generic, unrelated or promotional "
            "mentions",
        },
    ),
    LayaQuestion(
        dimension=QualificationDimension.ATTRIBUTION,
        kind=LayaDecisionKind.PROBABILITY,
        instructions="Do these events describe this specific company and its business?",
        criteria={
            "false": "the events are about another entity, a namesake or unrelated topics",
            "true": "the events are clearly about this company and its business",
        },
    ),
    LayaQuestion(
        dimension=QualificationDimension.COMPANY_FIT,
        kind=LayaDecisionKind.PROBABILITY,
        instructions="Is this company a good prospect right now for AI and automation "
        "services, based on concrete recent business activity?",
        criteria={
            "false": "evidence is generic, weak, unrelated or outdated; no concrete activity",
            "true": "concrete recent activity such as funding, expansion, deployments or AI "
            "adoption that creates demand",
        },
    ),
    LayaQuestion(
        dimension=QualificationDimension.EVIDENCE_QUALITY,
        kind=LayaDecisionKind.SCORE,
        instructions="How strong and specific is the evidence about this company?",
        criteria=[
            "insufficient: nothing concrete about the company",
            "partial: some concrete facts but thin or single-source",
            "sufficient: concrete, specific and recent facts",
        ],
    ),
)


class LayaVerdict(StrEnum):
    STRONG_POSITIVE = "strong_positive"
    NEGATIVE = "negative"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class LayaAssessment:
    trigger: str | None
    trigger_confidence: float | None
    attribution: float | None  # P(about this company)
    fit: float | None  # P(good prospect) - used as the decision score
    evidence_level: float | None  # 0..2 expected level
    verdict: LayaVerdict
    confidence: float  # min calibrated confidence across the answers used
    reasons: tuple[str, ...]
    model_version: str | None

    def as_record(self) -> dict[str, Any]:
        return {
            "trigger": self.trigger,
            "trigger_confidence": self.trigger_confidence,
            "attribution": self.attribution,
            "fit": self.fit,
            "evidence_level": self.evidence_level,
            "verdict": self.verdict.value,
            "confidence": self.confidence,
            "reasons": list(self.reasons),
            "model_version": self.model_version,
        }


def _num(result: QualificationResult, dim: QualificationDimension) -> tuple[float | None, float]:
    out = result.output(dim)
    if out is None or isinstance(out.value, str | bool):
        return None, 0.0
    return float(out.value), out.confidence if out.confidence is not None else 0.0


def interpret(result: QualificationResult, icp: IcpConfig) -> LayaAssessment:
    """Map typed answers to a verdict. Missing answers make the verdict AMBIGUOUS."""
    trig = result.output(QualificationDimension.BUSINESS_TRIGGER)
    trigger = trig.value if trig is not None and isinstance(trig.value, str) else None
    trig_conf = trig.confidence if trig is not None else None
    attribution, a_conf = _num(result, QualificationDimension.ATTRIBUTION)
    fit, f_conf = _num(result, QualificationDimension.COMPANY_FIT)
    level, l_conf = _num(result, QualificationDimension.EVIDENCE_QUALITY)
    hi, lo = icp.laya_qualify_threshold, icp.laya_reject_threshold
    reasons: list[str] = []
    if trigger is None or attribution is None or fit is None or level is None:
        verdict = LayaVerdict.AMBIGUOUS
        reasons.append("laya returned incomplete answers")
    else:
        if attribution <= lo:
            verdict = LayaVerdict.NEGATIVE
            reasons.append(f"laya: evidence likely not about this company (P={attribution:.2f})")
        elif fit <= lo:
            verdict = LayaVerdict.NEGATIVE
            reasons.append(f"laya: poor fit (P(fit)={fit:.2f})")
        elif trigger == Trigger.NONE and (trig_conf or 0) >= hi:
            verdict = LayaVerdict.NEGATIVE
            reasons.append("laya: no meaningful business trigger")
        elif fit >= hi and attribution >= hi and level >= 1.0 and trigger != Trigger.NONE:
            verdict = LayaVerdict.STRONG_POSITIVE
            reasons.append(
                f"laya: strong fit (P(fit)={fit:.2f}, attribution={attribution:.2f}, "
                f"trigger={trigger}, evidence level={level:.2f})"
            )
        else:
            verdict = LayaVerdict.AMBIGUOUS
            reasons.append(
                f"laya: ambiguous (P(fit)={fit:.2f}, attribution={attribution:.2f}, "
                f"trigger={trigger}, evidence level={level:.2f})"
            )
    confs = [c for c in (trig_conf, a_conf, f_conf, l_conf) if c is not None]
    return LayaAssessment(
        trigger=trigger,
        trigger_confidence=trig_conf,
        attribution=attribution,
        fit=fit,
        evidence_level=level,
        verdict=verdict,
        confidence=round(min(confs), 4) if confs else 0.0,
        reasons=tuple(reasons),
        model_version=result.model_version,
    )
