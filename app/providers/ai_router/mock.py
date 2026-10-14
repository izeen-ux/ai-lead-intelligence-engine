"""Deterministic offline Techno-AI-Router stand-in (PROVIDER_MODE=mock, tests, dry runs).

* ``review``   - never decides: REVIEW with the first evidence id (Phase 5 routing).
* ``complete`` - for lead-analysis prompts (user message = our JSON with ``evidence`` +
  ``catalog``) it returns a VALID, grounded analysis built only from the bundle (facts from
  event headlines, offer chosen by fixed type rules); for any other prompt a short greeting.
It is not a model: dry-run analyses show the pipeline, not real reasoning quality.
"""

from __future__ import annotations

import json
from typing import Any

from app.domain.enums import ProviderName, QualificationDecision
from app.providers.base import MockProviderBase
from app.providers.contracts import (
    ChatRequest,
    ChatResult,
    ChatUsage,
    LlmReviewRequest,
    LlmReviewResult,
)

MOCK_MODEL = "mock/lead-analyst-0"
_OFFER_BY_TYPE = (
    ("hiring", "ai_hiring_rekroots", "hiring_growth"),
    ("ai_interest", "enterprise_ai_implementation", "ai_automation"),
    ("technology", "enterprise_ai_implementation", "technology"),
    ("expansion", "ai_automation", "expansion"),
    ("product_customer", "ai_presales_agents", "customer_deployment"),
    ("funding", "ai_audit_general", "funding"),
)


def mock_analysis(user: dict[str, Any]) -> dict[str, Any]:
    ev = user["evidence"]
    events = [e for e in ev.get("events", []) if e.get("about_this_company", True)] or ev.get(
        "events", []
    )
    qual_id = ev["qualification"]["id"]
    first = events[0] if events else None
    types = set(first["types"]) if first else set()
    offer, trigger = next(
        ((o, t) for key, o, t in _OFFER_BY_TYPE if key in types), ("ai_audit_general", "other")
    )
    allowed_offers = {o["offer_id"] for o in user["catalog"]["offers"]}
    offer = offer if offer in allowed_offers else "ai_audit_general"
    cite = [first["id"]] if first else [qual_id]
    facts = [
        {"statement": f"Reported: {e['headline']}", "evidence_ids": [e["id"]]} for e in events[:3]
    ] or [
        {
            "statement": f"Qualified via the {ev['qualification']['path']} path",
            "evidence_ids": [qual_id],
        }
    ]
    company = ev["company"]["name"]
    return {
        "observed_facts": facts,
        "evidence_based_inferences": [
            {"statement": f"{company} is in an active growth phase", "evidence_ids": cite}
        ],
        "pain_hypotheses": [
            {
                "statement": f"Scaling may add repetitive operational work at {company}",
                "confidence": 0.5,
                "evidence_ids": cite,
            }
        ],
        "ai_opportunities": [
            {
                "description": "Automate one repetitive workflow tied to the growth event",
                "offer_id": offer,
                "evidence_ids": cite,
            }
        ],
        "primary_trigger": trigger,
        "recommended_offer_id": offer,
        "offer_match_reason": (
            f"Deterministic mock rule: event types {sorted(types) or ['none']} map to {offer}."
        ),
        "outreach_hook": (
            f"Saw the recent news about {company} - curious how the team is handling "
            "the extra workload."
        ),
        "confidence": 0.6,
        "supporting_evidence_ids": sorted({*cite, qual_id}),
    }


class MockAiRouterProvider(MockProviderBase):
    name = ProviderName.AI_ROUTER

    async def review(self, request: LlmReviewRequest) -> LlmReviewResult:
        self.calls.append("review")
        return LlmReviewResult(
            provider=self.name,
            decision=QualificationDecision.REVIEW,
            confidence=0.5,
            reasons=["mock reviewer: no opinion - human decision required"],
            supporting_evidence_ids=request.allowed_evidence_ids[:1],
            model="mock-llm-0",
        )

    async def complete(self, request: ChatRequest) -> ChatResult:
        self.calls.append("complete")
        user_text = next((m.content for m in request.messages if m.role == "user"), "")
        try:
            user = json.loads(user_text)
        except json.JSONDecodeError:
            user = None
        if isinstance(user, dict) and "evidence" in user and "catalog" in user:
            content = json.dumps(mock_analysis(user))
        else:
            content = "Hello from the mock AI router."
        prompt_tokens = sum(len(m.content) for m in request.messages) // 4
        completion_tokens = len(content) // 4
        return ChatResult(
            provider=self.name,
            content=content,
            finish_reason="stop",
            model=MOCK_MODEL,
            requested_model=request.model or MOCK_MODEL,
            gateway="mock",
            upstream_provider="mock",
            usage=ChatUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
            latency_ms=0,
            cost_usd=None,
        )
