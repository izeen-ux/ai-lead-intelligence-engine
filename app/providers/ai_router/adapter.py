"""DEPRECATED (Phase 10): the Techno-AI-Router is no longer the active reasoning provider.
OpenAI (app/providers/openai) is. This adapter is kept only for explicit opt-in
(REASONING_PROVIDER=ai_router) and so historical records stay explainable; it is never selected
or called by default.

Live Techno-AI-Router adapter - the single AI provider boundary (Phase 7).

* ``complete``  chat completion with the configured model policy:
    - the live catalog (GET /v1/models, no key) is authoritative: only ENABLED models of the
      configured gateway whose price (in AND out, USD / 1M tokens) is within
      ``AI_ROUTER_MAX_PRICE_PER_1M_TOKENS_USD`` are used (default 0 = free models only);
    - primary = AI_ROUTER_DEFAULT_MODEL if allowed, else the first allowed fallback, else the
      first allowed catalog model; allowed fallbacks are sent as the router ``fallback`` array;
    - bounded retry on 429 / 5xx / timeout; if the router reports the model unavailable, the
      next allowed model is tried client-side;
    - generation controls use the router's camelCase ``maxTokens`` (``max_tokens`` is ignored
      by the router per the guide) and ``temperature``.
* discovery helpers (``health``, ``list_providers``, ``list_models``) need no key and no paid
  switch.
* ``review`` (Phase 5 REVIEW second opinion) stays a disabled contract - out of Phase 7 scope.
Paid-guarded like every hosted LLM: PROVIDER_MODE=live + ALLOW_PAID_PROVIDER_CALLS=true + key.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

import httpx2

from app.core.config import Settings
from app.core.errors import (
    ProviderMalformedResponseError,
    ProviderModelUnavailableError,
    ProviderNotConfiguredError,
    ProviderNotImplementedError,
)
from app.domain.enums import ProviderName
from app.providers.ai_router.client import NAME, RouterClient, Sleep, base_url_problem
from app.providers.base import LiveAdapterBase
from app.providers.contracts import (
    ChatRequest,
    ChatResult,
    ChatUsage,
    LlmReviewRequest,
    LlmReviewResult,
    ProviderStatus,
    RouterModel,
)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


class AiRouterAdapter(LiveAdapterBase):
    name = ProviderName.AI_ROUTER
    implemented_in_phase = 7
    operational = True

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        sleep: Sleep | None = None,
    ) -> None:
        super().__init__(settings)
        self._client = (
            RouterClient(settings, transport=transport, sleep=sleep)
            if sleep is not None
            else RouterClient(settings, transport=transport)
        )
        self._catalog: list[RouterModel] | None = None

    # ------------------------------------------------------------------ status / guards
    def status(self) -> ProviderStatus:
        base = super().status()
        problem = base_url_problem(self._settings) or (
            None if base.configured else "AI_ROUTER_API_KEY not set"
        )
        return base.model_copy(
            update={
                "configured": problem is None,
                "operational": problem is None,
                "detail": problem,
            }
        )

    def _ensure_callable(self, operation: str) -> None:
        super()._ensure_callable(operation)  # paid guard -> key present
        problem = base_url_problem(self._settings)
        if problem is not None:
            raise ProviderNotConfiguredError(self.name.value, f"{operation}: {problem}")

    # ------------------------------------------------------------------ discovery (no key)
    async def health(self) -> dict[str, Any]:
        return await self._client.health()

    async def list_providers(self) -> list[dict[str, Any]]:
        return await self._client.providers(enabled_only=True)

    async def list_models(self, *, refresh: bool = False) -> list[RouterModel]:
        if self._catalog is None or refresh:
            self._catalog = await self._client.models(provider=self._settings.ai_router_gateway)
        return self._catalog

    def _within_ceiling(self, m: RouterModel) -> bool:
        ceiling = self._settings.ai_router_max_price_per_1m_tokens_usd
        if m.price_in_per_1m_usd is None or m.price_out_per_1m_usd is None:
            return False  # unknown price never passes a cost ceiling
        return m.price_in_per_1m_usd <= ceiling and m.price_out_per_1m_usd <= ceiling

    async def allowed_models(self) -> list[RouterModel]:
        gateway = self._settings.ai_router_gateway
        return [
            m
            for m in await self.list_models()
            if m.enabled
            and m.modality == "llm"
            and m.provider == gateway
            and self._within_ceiling(m)
        ]

    async def candidate_models(self, requested: str | None = None) -> list[RouterModel]:
        """Ordered models to try: primary first, then allowed fallbacks (never above ceiling)."""
        gateway = self._settings.ai_router_gateway
        allowed = {m.model: m for m in await self.allowed_models()}
        wanted = [requested or self._settings.ai_router_default_model or ""]
        for entry in self._settings.ai_router_fallback_models.split(","):
            entry = entry.strip()
            if entry:
                gw, _, model = entry.partition("/")
                wanted.append(model if gw == gateway else "")
        ordered = [allowed[w] for w in dict.fromkeys(wanted) if w in allowed]
        if not ordered and not any(wanted) and allowed:
            # nothing configured: recommended model(s) first, then the next allowed ones as
            # fallbacks (at most 3 in total) - the live catalog decides, never a hard-coded id
            ranked = sorted(allowed.values(), key=lambda m: "recommended" not in m.tags)
            ordered = ranked[:3]
        if not ordered:
            raise ProviderModelUnavailableError(
                self.name.value,
                "no configured model is enabled on the router within the cost ceiling "
                f"(gateway={gateway}, "
                f"ceiling=${self._settings.ai_router_max_price_per_1m_tokens_usd}/1M)",
            )
        return ordered

    # ------------------------------------------------------------------ chat (key)
    async def complete(self, request: ChatRequest) -> ChatResult:
        self._ensure_callable("complete")
        candidates = await self.candidate_models(request.model)
        gateway = self._settings.ai_router_gateway
        last_error: ProviderModelUnavailableError | None = None
        attempts = 0
        for i, model in enumerate(candidates):
            body: dict[str, Any] = {
                "gateway": gateway,
                "model": model.model,
                "messages": [m.model_dump() for m in request.messages],
                "maxTokens": self._settings.ai_router_max_tokens,
                "temperature": self._settings.ai_router_temperature,
            }
            backups = [f"{gateway}/{m.model}" for m in candidates[i + 1 :]]
            if backups:
                body["fallback"] = backups
            started = time.perf_counter()
            try:
                data, tries = await self._client.chat(
                    body, retries=self._settings.ai_router_max_retries
                )
            except ProviderModelUnavailableError as exc:
                attempts += 1
                last_error = exc
                continue  # client-side fallback to the next allowed model
            attempts += tries
            return self._result(
                data, model, candidates, attempts, int((time.perf_counter() - started) * 1000)
            )
        assert last_error is not None  # noqa: S101 - loop ran at least once
        raise last_error

    def _result(self, data: dict[str, Any], requested: RouterModel, candidates: list[RouterModel],
                attempts: int, latency_ms: int) -> ChatResult:  # fmt: skip
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderMalformedResponseError(NAME, "router response has no choices")
        message = choices[0].get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise ProviderMalformedResponseError(NAME, "router response has no message content")
        raw_usage = _dict(data.get("usage"))
        usage = ChatUsage(
            prompt_tokens=_int(raw_usage.get("prompt_tokens")),
            completion_tokens=_int(raw_usage.get("completion_tokens")),
            total_tokens=_int(raw_usage.get("total_tokens")),
        )
        x_router = _dict(data.get("x_router"))
        answered = _str(data.get("model")) or requested.model
        priced = next((m for m in candidates if m.model == answered), requested)
        cost = None
        if priced.price_in_per_1m_usd is not None and priced.price_out_per_1m_usd is not None:
            cost = (
                priced.price_in_per_1m_usd * usage.prompt_tokens
                + priced.price_out_per_1m_usd * usage.completion_tokens
            ) / Decimal(1_000_000)
        first = _dict(choices[0])
        return ChatResult(
            provider=self.name,
            content=content,
            finish_reason=_str(first.get("finish_reason")),
            model=answered,
            requested_model=requested.model,
            gateway=_str(x_router.get("gateway")),
            upstream_provider=_str(x_router.get("provider")),
            usage=usage,
            latency_ms=latency_ms,
            attempts=max(attempts, 1),
            cost_usd=cost,
        )

    async def review(self, request: LlmReviewRequest) -> LlmReviewResult:
        """Phase 5 REVIEW second opinion: still a disabled contract (outside Phase 7)."""
        self._ensure_callable("review")
        raise ProviderNotImplementedError(
            self.name.value,
            "ai_router.review (Phase 5 REVIEW second opinion) is not enabled - outside Phase 7",
            details={"operation": "review", "phase": 5},
        )
