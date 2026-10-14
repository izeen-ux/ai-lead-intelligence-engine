"""Deterministic offline Laya stand-in (PROVIDER_MODE=mock, tests, dry runs).

It answers the same typed questions from the bundle's structure (attributed events, types,
publisher counts) with fixed rules. It is NOT a model and says nothing about Laya's accuracy;
evaluation numbers produced with it measure the deterministic pipeline only.
"""

from __future__ import annotations

from typing import Any

from app.domain.enums import LayaDecisionKind, ProviderName
from app.domain.models import LayaOutput
from app.providers.base import MockProviderBase
from app.providers.contracts import LayaQuestion, QualificationRequest, QualificationResult

_TRIGGER_BY_TYPE = (
    ("funding", "funding"),
    ("ai_interest", "ai_automation"),
    ("technology", "ai_automation"),
    ("expansion", "expansion"),
    ("product_customer", "customer_deployment"),
    ("hiring", "hiring_growth"),
)


def _heuristics(state: dict[str, Any]) -> dict[str, float | str]:
    events = [e for e in state.get("events") or [] if isinstance(e, dict)]
    attributed = [e for e in events if e.get("attributed")]
    substantive = [e for e in attributed if set(e.get("types") or []) - {"hiring", "other"}]
    trigger = "none"
    if substantive:
        types = set(substantive[0].get("types") or [])
        trigger = next((t for key, t in _TRIGGER_BY_TYPE if key in types), "none")
    fit = min(0.5 + 0.2 * len(substantive), 0.9) if substantive else 0.2
    return {
        "trigger": trigger,
        "attribution": 0.9 if attributed else 0.2,
        "fit": round(fit, 3),
        "level": float(min(len(substantive), 2)),
    }


class MockLayaProvider(MockProviderBase):
    name = ProviderName.LAYA

    async def evaluate(self, request: QualificationRequest) -> QualificationResult:
        self.calls.append("evaluate")
        h = _heuristics(request.state)
        return QualificationResult(
            provider=self.name,
            outputs=[self._answer(q, h) for q in request.questions],
            model_version="mock-laya-0",
            latency_ms=0,
        )

    @staticmethod
    def _answer(q: LayaQuestion, h: dict[str, float | str]) -> LayaOutput:
        if q.kind is LayaDecisionKind.CHOICE:
            assert isinstance(q.criteria, dict)  # noqa: S101
            labels = list(q.criteria)
            choice = str(h["trigger"]) if h["trigger"] in labels else labels[-1]
            return LayaOutput(dimension=q.dimension, kind=q.kind, value=choice, confidence=0.8)
        if q.kind is LayaDecisionKind.SCORE:
            top = len(q.criteria) - 1
            level = min(float(h["level"]), float(top))
            return LayaOutput(dimension=q.dimension, kind=q.kind, value=level, confidence=0.8)
        key = "attribution" if q.dimension.value == "attribution" else "fit"
        p = float(h[key])
        return LayaOutput(
            dimension=q.dimension, kind=q.kind, value=p, confidence=round(max(p, 1 - p), 3)
        )
