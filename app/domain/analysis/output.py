"""Strict structured analysis output + grounding validation.

Sections are kept distinct on purpose:
  FACT            ``observed_facts``             - stated in the evidence; must cite ids
  INFERENCE       ``evidence_based_inferences``  - reasoned from cited facts
  HYPOTHESIS      ``pain_hypotheses``            - plausible but unconfirmed; has a confidence
  RECOMMENDATION  ``ai_opportunities`` + ``recommended_offer_id`` + ``outreach_hook``
Validation rejects: malformed JSON, missing / extra fields, evidence ids not in the bundle,
offer ids not in the catalog, a hypothesis restated as a fact, contact data in the hook.
"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import Field, ValidationError

from app.domain.analysis.bundle import AnalysisBundle
from app.domain.analysis.catalog import ServiceCatalog
from app.domain.models import DomainModel

Trigger = Literal[
    "funding",
    "ai_automation",
    "expansion",
    "customer_deployment",
    "hiring_growth",
    "technology",
    "other",
]
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_PHONE = re.compile(r"\+?\d[\d\s().-]{8,}\d")


class Fact(DomainModel):
    statement: str = Field(min_length=5, max_length=400)
    evidence_ids: list[str] = Field(min_length=1, max_length=6)


class Inference(DomainModel):
    statement: str = Field(min_length=5, max_length=400)
    evidence_ids: list[str] = Field(min_length=1, max_length=6)


class Hypothesis(DomainModel):
    statement: str = Field(min_length=5, max_length=400)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_ids: list[str] = Field(min_length=1, max_length=6)


class Opportunity(DomainModel):
    description: str = Field(min_length=5, max_length=400)
    offer_id: str | None = None
    evidence_ids: list[str] = Field(min_length=1, max_length=6)


class LeadAnalysisOutput(DomainModel):
    observed_facts: list[Fact] = Field(min_length=1, max_length=8)
    evidence_based_inferences: list[Inference] = Field(default_factory=list, max_length=6)
    pain_hypotheses: list[Hypothesis] = Field(min_length=1, max_length=5)
    ai_opportunities: list[Opportunity] = Field(min_length=1, max_length=5)
    primary_trigger: Trigger
    recommended_offer_id: str = Field(min_length=3, max_length=64)
    offer_match_reason: str = Field(min_length=10, max_length=600)
    outreach_hook: str = Field(min_length=10, max_length=400)
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_evidence_ids: list[str] = Field(min_length=1, max_length=12)

    def cited_ids(self) -> set[str]:
        ids = set(self.supporting_evidence_ids)
        for group in (
            self.observed_facts,
            self.evidence_based_inferences,
            self.pain_hypotheses,
            self.ai_opportunities,
        ):
            for item in group:
                ids.update(item.evidence_ids)
        return ids


class AnalysisOutputError(ValueError):
    """The model's answer could not be accepted. ``kind`` is stored with the record."""

    def __init__(self, kind: str, problems: list[str]) -> None:
        super().__init__(f"{kind}: " + "; ".join(problems))
        self.kind = kind
        self.problems = problems


def _json_text(content: str) -> str:
    text = content.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        return fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise AnalysisOutputError("malformed_json", ["no JSON object in the response"])
    return text[start : end + 1]


def parse_output(content: str) -> LeadAnalysisOutput:
    try:
        data = json.loads(_json_text(content))
    except json.JSONDecodeError as exc:
        raise AnalysisOutputError("malformed_json", [f"invalid JSON: {exc.msg}"]) from None
    try:
        return LeadAnalysisOutput.model_validate(data)
    except ValidationError as exc:
        problems = [f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()[:10]]
        raise AnalysisOutputError("schema_violation", problems) from None


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def validate_output(
    out: LeadAnalysisOutput, bundle: AnalysisBundle, catalog: ServiceCatalog
) -> None:
    problems: list[str] = []
    unknown = sorted(out.cited_ids() - bundle.allowed_ids)
    if unknown:
        problems.append(f"unsupported evidence ids: {unknown}")
    if out.recommended_offer_id not in catalog.offer_ids:
        problems.append(f"recommended_offer_id {out.recommended_offer_id!r} is not in the catalog")
    for opp in out.ai_opportunities:
        if opp.offer_id is not None and opp.offer_id not in catalog.offer_ids:
            problems.append(f"opportunity offer_id {opp.offer_id!r} is not in the catalog")
    contact_ids = {i for i in bundle.allowed_ids if i.startswith("contact_")}
    for fact in out.observed_facts:
        if set(fact.evidence_ids) <= contact_ids:
            problems.append(f"fact cites only the contact record: {fact.statement[:60]!r}")
    facts = {_norm(f.statement) for f in out.observed_facts}
    for hyp in out.pain_hypotheses:
        if _norm(hyp.statement) in facts:
            problems.append(f"hypothesis presented as a fact: {hyp.statement[:60]!r}")
    if _EMAIL.search(out.outreach_hook) or _PHONE.search(out.outreach_hook):
        problems.append("outreach_hook contains contact details")
    if problems:
        kind = (
            "unsupported_evidence"
            if unknown
            else (
                "invalid_offer" if any("catalog" in p for p in problems) else "grounding_violation"
            )
        )
        raise AnalysisOutputError(kind, problems)
