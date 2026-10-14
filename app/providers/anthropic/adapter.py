"""Claude (Anthropic) as the reasoning provider (owner decision 2026-10-14).

Same boundary as the OpenAI adapter - ``complete`` (one bounded, non-streaming answer, JSON
when a schema is given) and ``review`` (AI review of REVIEW qualification cases) - through the
official ``anthropic`` SDK, Messages API. Selected with ``REASONING_PROVIDER=anthropic``.

  * Structured answers use ``output_config.format`` (JSON schema); the domain validation in
    the services stays authoritative.
  * Thinking is adaptive (always on for the default model); depth is set with
    ``ANTHROPIC_EFFORT``.
  * A policy refusal is retried server-side on Anthropic's recommended fallback model
    (``fallbacks="default"``) for the models that support it; a refusal that remains is a
    typed error, never an empty answer.
  * Spend is bounded before the call (worst-case cost vs the per-run budget) and the key is
    never logged.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, ClassVar

import anthropic
from anthropic import AsyncAnthropic

from app.core.config import Settings
from app.core.errors import (
    ProviderBudgetExceededError,
    ProviderError,
    ProviderIncompleteResponseError,
    ProviderMalformedResponseError,
    ProviderModelUnavailableError,
    ProviderNotConfiguredError,
    ProviderQuotaExhaustedError,
    ProviderRateLimitedError,
    ProviderRefusalError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.core.logging import get_logger
from app.domain.enums import ProviderName
from app.providers.base import LiveAdapterBase
from app.providers.contracts import (
    ChatMessage,
    ChatRequest,
    ChatResult,
    ChatUsage,
    LlmReviewRequest,
    LlmReviewResult,
    ProviderStatus,
)
from app.providers.openai.review import REVIEW_SCHEMA, parse_review, review_messages

logger = get_logger(__name__)

PROVIDER = ProviderName.ANTHROPIC.value
MILLION = Decimal(1_000_000)
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that accept the server-side refusal fallback in its "default" form.
FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5")

# USD per 1M tokens (input, output). Override with ANTHROPIC_PRICE_*_PER_1M_USD.
KNOWN_PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "claude-opus-5-5": (Decimal("4.00"), Decimal("20.00")),
    "claude-opus-5": (Decimal("5.00"), Decimal("25.00")),
    "claude-sonnet-5-5": (Decimal("2.00"), Decimal("10.00")),
    "claude-sonnet-5": (Decimal("2.00"), Decimal("10.00")),
    "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50")),
    "claude-fable-5-1": (Decimal("10.00"), Decimal("50.00")),
}


def estimate_tokens(text: str) -> int:
    """Conservative size estimate for the pre-call budget check (about 3 characters/token)."""
    return len(text) // 3 + 1


class AnthropicAdapter(LiveAdapterBase):
    name: ClassVar[ProviderName] = ProviderName.ANTHROPIC
    implemented_in_phase: ClassVar[int] = 10
    operational: ClassVar[bool] = True

    def __init__(self, settings: Settings, *, client: Any | None = None) -> None:
        super().__init__(settings)
        self._client: Any | None = client  # injected in tests (never the network)
        self._model_checked = False

    # ------------------------------------------------------------------ config
    @property
    def model(self) -> str:
        return self._settings.anthropic_model

    def prices(self) -> tuple[Decimal, Decimal] | None:
        s = self._settings
        if (
            s.anthropic_price_input_per_1m_usd is not None
            and s.anthropic_price_output_per_1m_usd is not None
        ):
            return s.anthropic_price_input_per_1m_usd, s.anthropic_price_output_per_1m_usd
        return KNOWN_PRICES.get(self.model)

    def cost(self, input_tokens: int, output_tokens: int) -> Decimal | None:
        prices = self.prices()
        if prices is None:
            return None
        value = (Decimal(input_tokens) * prices[0] + Decimal(output_tokens) * prices[1]) / MILLION
        return value.quantize(Decimal("0.000001"))

    def status(self) -> ProviderStatus:
        configured = self._settings.provider_has_credentials(self.name)
        detail = f"model {self.model}" if configured else "ANTHROPIC_API_KEY not set"
        if configured and self.prices() is None:
            detail += " (price unknown: set ANTHROPIC_PRICE_*_PER_1M_USD)"
        return ProviderStatus(
            name=self.name,
            mode="live",
            configured=configured,
            operational=configured and self.prices() is not None,
            paid=True,
            detail=detail,
        )

    def _sdk(self) -> Any:
        if self._client is None:
            key = self._settings.anthropic_api_key
            if key is None:  # pragma: no cover - _ensure_callable already checked
                raise ProviderNotConfiguredError(PROVIDER, "ANTHROPIC_API_KEY missing")
            self._client = AsyncAnthropic(
                api_key=key.get_secret_value(),
                timeout=self._settings.anthropic_timeout_seconds,
                max_retries=self._settings.anthropic_max_retries,  # 408/429/5xx, with backoff
            )
        return self._client

    # ------------------------------------------------------------------ errors
    @staticmethod
    def _map(exc: anthropic.AnthropicError) -> ProviderError:
        """SDK exception -> typed project error. Messages never include the key."""
        if isinstance(exc, anthropic.APITimeoutError):
            return ProviderTimeoutError(PROVIDER, "Claude request timed out")
        if isinstance(exc, anthropic.APIConnectionError):
            return ProviderUnavailableError(PROVIDER, "Claude API unreachable")
        if isinstance(exc, anthropic.APIStatusError):
            status = exc.status_code
            kind = str(getattr(exc, "type", "") or "")
            details = {
                "status": status,
                "error_type": kind,
                "request_id": getattr(exc, "request_id", None),
            }
            if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
                return ProviderNotConfiguredError(
                    PROVIDER,
                    f"Anthropic rejected the API key or its permissions (HTTP {status}) - check "
                    "ANTHROPIC_API_KEY (a Claude API key starts with sk-ant-)",
                    details=details,
                )
            if status == 402 or kind == "billing_error":
                return ProviderQuotaExhaustedError(
                    PROVIDER,
                    "Anthropic: billing problem / no credits on this API key (add credits in the "
                    "Claude Console)",
                    details=details,
                )
            if isinstance(exc, anthropic.NotFoundError):
                return ProviderModelUnavailableError(
                    PROVIDER, "Claude model not found or not available to this key", details=details
                )
            if isinstance(exc, anthropic.RateLimitError):
                return ProviderRateLimitedError(PROVIDER, "Claude rate limit", details=details)
            if status >= 500:
                return ProviderUnavailableError(
                    PROVIDER, f"Claude server error {status}", details=details
                )
            return ProviderError(
                PROVIDER,
                f"Anthropic rejected the request (HTTP {status}, {kind or 'error'})",
                details=details,
            )
        return ProviderMalformedResponseError(
            PROVIDER,
            "unexpected Anthropic client error",
            details={"error_type": type(exc).__name__},
        )

    # ------------------------------------------------------------------ calls
    async def check_model(self) -> dict[str, Any]:
        """Free metadata call: the configured model exists for this key. No generation."""
        self._ensure_callable("check_model")
        try:
            info = await self._sdk().models.retrieve(self.model)
        except anthropic.AnthropicError as exc:
            raise self._map(exc) from None
        self._model_checked = True
        return {"id": info.id, "display_name": getattr(info, "display_name", None)}

    def _precheck_budget(self, request: ChatRequest) -> None:
        prices = self.prices()
        if prices is None:
            raise ProviderNotConfiguredError(
                PROVIDER,
                f"no price known for model {self.model!r}: set ANTHROPIC_PRICE_INPUT_PER_1M_USD "
                "and ANTHROPIC_PRICE_OUTPUT_PER_1M_USD so spend can be budgeted",
            )
        prompt = sum(estimate_tokens(m.content) for m in request.messages)
        worst = self.cost(prompt, self._settings.anthropic_max_tokens) or Decimal(0)
        cap = self._settings.anthropic_max_estimated_cost_usd_per_run
        if worst > cap:
            raise ProviderBudgetExceededError(
                PROVIDER,
                f"worst-case cost of one call (${worst}) exceeds the per-run AI budget (${cap})",
                details={"worst_case_usd": str(worst), "budget_usd": str(cap)},
            )

    def _kwargs(self, request: ChatRequest) -> dict[str, Any]:
        model = request.model or self.model
        system = "\n\n".join(m.content for m in request.messages if m.role == "system")
        turns = [
            {"role": m.role, "content": m.content} for m in request.messages if m.role != "system"
        ]
        output: dict[str, Any] = {"effort": self._settings.anthropic_effort}
        if request.response_schema is not None:
            output["format"] = {
                "type": "json_schema",
                "schema": anthropic.transform_schema(request.response_schema),
            }
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": self._settings.anthropic_max_tokens,
            "messages": turns,
            "output_config": output,
        }
        if system:
            kwargs["system"] = system
        if self._settings.anthropic_refusal_fallback and model.startswith(FALLBACK_MODELS):
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        return kwargs

    async def complete(self, request: ChatRequest) -> ChatResult:
        self._ensure_callable("complete")
        self._precheck_budget(request)
        kwargs = self._kwargs(request)
        started = time.monotonic()
        try:
            response = await self._sdk().beta.messages.create(**kwargs)
        except anthropic.AnthropicError as exc:
            error = self._map(exc)
            self._log(request, kwargs["model"], error.code, started, None, None)
            raise error from None
        return self._result(request, kwargs["model"], response, started)

    def _log(
        self,
        request: ChatRequest,
        model: str,
        outcome: str,
        started: float,
        usage: ChatUsage | None,
        cost: Decimal | None,
    ) -> None:
        logger.info(
            "claude call",
            extra={
                "purpose": request.purpose,
                "model": model,
                "outcome": outcome,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "prompt_tokens": usage.prompt_tokens if usage else None,
                "completion_tokens": usage.completion_tokens if usage else None,
                "cost_usd": str(cost) if cost is not None else None,
            },
        )

    def _result(
        self, request: ChatRequest, model: str, response: Any, started: float
    ) -> ChatResult:
        raw = getattr(response, "usage", None)
        prompt = sum(
            int(getattr(raw, name, 0) or 0)
            for name in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        )
        completion = int(getattr(raw, "output_tokens", 0) or 0)
        usage = ChatUsage(
            prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion
        )
        cost = self.cost(prompt, completion)
        spent = {"usage": usage.model_dump(), "cost_usd": str(cost) if cost is not None else None}
        answered = str(getattr(response, "model", "") or model)
        stop = str(getattr(response, "stop_reason", "") or "")

        if stop == "refusal":  # checked before reading content
            category = getattr(getattr(response, "stop_details", None), "category", None)
            self._log(request, answered, "refused", started, usage, cost)
            raise ProviderRefusalError(
                PROVIDER,
                "the model declined to answer",
                details={**spent, "model": answered, "category": category},
            )
        if stop == "max_tokens":
            self._log(request, answered, "incomplete", started, usage, cost)
            raise ProviderIncompleteResponseError(
                PROVIDER,
                "Claude answer cut off at ANTHROPIC_MAX_TOKENS",
                details={**spent, "model": answered, "reason": stop},
            )
        text = "".join(
            str(getattr(block, "text", "") or "")
            for block in (getattr(response, "content", None) or [])
            if getattr(block, "type", None) == "text"
        ).strip()
        if not text:
            self._log(request, answered, "malformed", started, usage, cost)
            raise ProviderMalformedResponseError(
                PROVIDER,
                "Claude returned no usable text",
                details={**spent, "model": answered, "stop_reason": stop},
            )
        self._log(request, answered, "ok", started, usage, cost)
        return ChatResult(
            provider=self.name,
            content=text,
            finish_reason="stop",
            model=answered,
            requested_model=model,
            gateway="anthropic",
            upstream_provider="anthropic",
            usage=usage,
            latency_ms=int((time.monotonic() - started) * 1000),
            attempts=1,
            cost_usd=cost,
        )

    async def review(self, request: LlmReviewRequest) -> LlmReviewResult:
        """AI review of a REVIEW case: attribution of funding news or a second opinion. One
        bounded call; the policy layer validates confidence and cited ids."""
        self._ensure_callable("review")
        result = await self.complete(
            ChatRequest(
                messages=[ChatMessage(**m) for m in review_messages(request)],
                purpose=f"review_{request.task}",
                response_schema=REVIEW_SCHEMA,
                response_schema_name="lead_review",
            )
        )
        return parse_review(result.content, result.model, provider=ProviderName.ANTHROPIC)
