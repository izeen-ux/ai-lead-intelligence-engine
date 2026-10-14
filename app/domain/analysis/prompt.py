"""Versioned lead-analysis prompt (``PROMPT_VERSION``). The user message is pure JSON
(evidence bundle + catalog + output contract) so it can be audited and replayed."""

from __future__ import annotations

import json
from typing import Any

from app.domain.analysis.bundle import AnalysisBundle
from app.domain.analysis.catalog import ServiceCatalog
from app.domain.analysis.config import PROMPT_VERSION

SYSTEM_PROMPT = """You are a careful B2B sales analyst for TechnoTribes, an AI services company.
Analyse ONE company using ONLY the JSON evidence you are given. Rules:
1. FACTS: only statements directly supported by the cited evidence. Cite evidence ids.
2. INFERENCES: conclusions reasoned from cited facts; cite the ids they rest on.
3. HYPOTHESES: plausible pains that are NOT confirmed; give each a confidence 0-1 and cite the
   evidence that suggests it. Never restate a hypothesis as a fact.
4. RECOMMENDATION: choose exactly one offer_id from the catalog. Never invent services, prices,
   durations, case studies, client names or guarantees. Respect each offer's constraints and
   evidence requirements; if no specialised offer fits, use "ai_audit_general".
5. The outreach hook is one or two sentences, references a cited event, makes no claims beyond
   the evidence and contains no email address or phone number.
6. Cite ONLY ids that appear in the evidence (qual_..., evt_..., page_..., contact_...).
7. Answer with ONE JSON object exactly matching "output_contract" - no prose, no markdown."""

OUTPUT_CONTRACT: dict[str, Any] = {
    "observed_facts": [{"statement": "string", "evidence_ids": ["evt_..."]}],
    "evidence_based_inferences": [{"statement": "string", "evidence_ids": ["evt_..."]}],
    "pain_hypotheses": [{"statement": "string", "confidence": 0.5, "evidence_ids": ["evt_..."]}],
    "ai_opportunities": [
        {
            "description": "string",
            "offer_id": "one catalog offer_id or null",
            "evidence_ids": ["evt_..."],
        }
    ],
    "primary_trigger": (
        "funding|ai_automation|expansion|customer_deployment|hiring_growth|technology|other"
    ),
    "recommended_offer_id": "one catalog offer_id",
    "offer_match_reason": "string",
    "outreach_hook": "string",
    "confidence": 0.5,
    "supporting_evidence_ids": ["evt_..."],
}


def build_messages(bundle: AnalysisBundle, catalog: ServiceCatalog) -> list[dict[str, str]]:
    user = {
        "prompt_version": PROMPT_VERSION,
        "task": "Analyse this company and recommend one offer from the catalog.",
        "evidence": bundle.payload,
        "catalog": {
            "catalog_version": catalog.version,
            "global_constraints": catalog.global_constraints,
            "offers": catalog.for_prompt(),
        },
        "output_contract": OUTPUT_CONTRACT,
    }
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]
