"""OpenAI adapter (Phase 10): the active ``ReasoningProvider``.

  official ``openai`` SDK -> ``AsyncOpenAI.responses.create`` (Responses API)
  structured output       -> ``text.format`` json_schema, strict (see ``schema.py``)
  no tools, no web search, ``store=False``; evidence comes from the app's own pipeline

Safety order before any generation: paid guard (``ALLOW_PAID_PROVIDER_CALLS``) -> key ->
known price for the model -> worst-case cost of THIS call within the per-run budget -> model
exists (one free ``models.retrieve`` per adapter instance).

Errors are mapped to the project's typed taxonomy; only transient ones (429 rate limit, 5xx,
timeout, connection) are retried, with bounded backoff. The API key, prompts and model output
are never logged - only provider, model, status, duration, token counts and estimated cost.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any, ClassVar

import httpx
import openai
from openai import AsyncOpenAI

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
from app.providers.openai.schema import text_format

logger = get_logger(__name__)
PROVIDER = ProviderName.OPENAI.value
MILLION = Decimal(1_000_000)
RETRYABLE = (ProviderRateLimitedError, ProviderUnavailableError, ProviderTimeoutError)
_NO_CREDIT_CODES = ("insufficient_quota", "billing_hard_limit_reached", "credit_balance_exhausted")

# USD per 1M tokens (input, output), standard tier. Verified 2026-10-05 against
# developers.openai.com/api/docs/pricing. Override with OPENAI_PRICE_*_PER_1M_USD.
KNOWN_PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "gpt-6-luna": (Decimal("0.10"), Decimal("0.50")),
    "gpt-5.6-luna": (Decimal("0.20"), Decimal("1.20")),
    "gpt-5.4-nano": (Decimal("0.20"), Decimal("1.25")),
    "gpt-5.4-mini": (Decimal("0.75"), Decimal("4.50")),
    "gpt-5-nano": (Decimal("0.05"), Decimal("0.40")),
    "gpt-5-mini": (Decimal("0.25"), Decimal("2.00")),
    "gpt-4.1-nano": (Decimal("0.10"), Decimal("0.40")),
    "gpt-4.1-mini": (Decimal("0.40"), Decimal("1.60")),
}


def estimate_tokens(text: str) -> int:
    """Conservative size estimate for the pre-call budget check (about 3 characters/token)."""
    return len(text) // 3 + 1


class OpenAIAdapter(LiveAdapterBase):
    name: ClassVar[ProviderName] = ProviderName.OPENAI
    implemented_in_phase: ClassVar[int] = 10
    operational: ClassVar[bool] = True

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        super().__init__(settings)
        self._http_client = http_client
        self._sleep = sleep
        self._client: AsyncOpenAI | None = None
        self._model_checked = False

    # ------------------------------------------------------------------ config
    @property
    def model(self) -> str:
        return self._settings.openai_model

    def prices(self) -> tuple[Decimal, Decimal] | None:
        s = self._settings
        if (
            s.openai_price_input_per_1m_usd is not None
            and s.openai_price_output_per_1m_usd is not None
        ):
            return s.openai_price_input_per_1m_usd, s.openai_price_output_per_1m_usd
        return KNOWN_PRICES.get(self.model)

    def cost(self, input_tokens: int, output_tokens: int) -> Decimal | None:
        prices = self.prices()
        if prices is None:
            return None
        value = (Decimal(input_tokens) * prices[0] + Decimal(output_tokens) * prices[1]) / MILLION
        return value.quantize(Decimal("0.000001"))

    def status(self) -> ProviderStatus:
        configured = self._settings.provider_has_credentials(self.name)
        detail = f"model {self.model}" if configured else "OPENAI_API_KEY not set"
        if configured and self.prices() is None:
            detail += " (price unknown: set OPENAI_PRICE_*_PER_1M_USD)"
        return ProviderStatus(
            name=self.name,
            mode="live",
            configured=configured,
            operational=configured and self.prices() is not None,
            paid=True,
            detail=detail,
        )

    def _sdk(self) -> AsyncOpenAI:
        if self._client is None:
            key = self._settings.openai_api_key
            if key is None or not key.get_secret_value().strip():
                raise ProviderNotConfiguredError(PROVIDER, "OPENAI_API_KEY is not set")
            self._client = AsyncOpenAI(
                api_key=key.get_secret_value(),
                base_url=self._settings.openai_base_url,
                timeout=self._settings.openai_timeout_seconds,
                max_retries=0,  # retries are done here: bounded, typed, testable
                http_client=self._http_client,
            )
        return self._client

    # ------------------------------------------------------------------ errors
    @staticmethod
    def _map(exc: openai.OpenAIError) -> ProviderError:
        """SDK exception -> typed project error. Messages never include the key."""
        if isinstance(exc, openai.APITimeoutError):
            return ProviderTimeoutError(PROVIDER, "OpenAI request timed out")
        if isinstance(exc, openai.APIConnectionError):
            return ProviderUnavailableError(PROVIDER, "OpenAI unreachable")
        if isinstance(exc, openai.APIStatusError):
            status = exc.status_code
            code = str(getattr(exc, "code", "") or "")
            details = {"status": status, "error_code": code, "request_id": exc.request_id}
            if status in (401, 403):
                return ProviderNotConfiguredError(
                    PROVIDER,
                    f"OpenAI rejected the API key or its permissions (HTTP {status})",
                    details=details,
                )
            if status == 404 or code in ("model_not_found", "unsupported_model"):
                return ProviderModelUnavailableError(
                    PROVIDER, "OpenAI model not found or not available to this key", details=details
                )
            if status == 429 and (
                code in _NO_CREDIT_CODES or getattr(exc, "type", None) == "insufficient_quota"
            ):
                return ProviderQuotaExhaustedError(
                    PROVIDER,
                    "OpenAI: this API key has no credits / hit its billing limit "
                    "(add credits or use another key in OPENAI_API_KEY)",
                    details=details,
                )
            if status == 429:
                return ProviderRateLimitedError(PROVIDER, "OpenAI rate limit", details=details)
            if status >= 500:
                return ProviderUnavailableError(
                    PROVIDER, f"OpenAI server error {status}", details=details
                )
            if status == 400 and "model" in str(getattr(exc, "param", "") or ""):
                return ProviderModelUnavailableError(
                    PROVIDER, "OpenAI rejected the configured model", details=details
                )
            return ProviderError(
                PROVIDER, f"OpenAI rejected the request (HTTP {status}, {code or 'error'})",
                details=details,
            )  # fmt: skip
        return ProviderMalformedResponseError(
            PROVIDER, "unexpected OpenAI client error", details={"error_type": type(exc).__name__}
        )

    # ------------------------------------------------------------------ calls
    async def check_model(self) -> dict[str, Any]:
        """Free metadata call: the configured model exists for this key. No generation."""
        if not self._settings.provider_has_credentials(self.name):
            raise ProviderNotConfiguredError(PROVIDER, "OPENAI_API_KEY is not set")
        try:
            info = await self._sdk().models.retrieve(self.model)
        except openai.OpenAIError as exc:
            raise self._map(exc) from None
        self._model_checked = True
        return {"id": info.id, "owned_by": getattr(info, "owned_by", None)}

    def _precheck_budget(self, request: ChatRequest) -> None:
        prices = self.prices()
        if prices is None:
            raise ProviderNotConfiguredError(
                PROVIDER,
                f"no price known for model {self.model!r}: set OPENAI_PRICE_INPUT_PER_1M_USD and "
                "OPENAI_PRICE_OUTPUT_PER_1M_USD so spend can be budgeted",
            )
        prompt = sum(estimate_tokens(m.content) for m in request.messages)
        worst = self.cost(prompt, self._settings.openai_max_tokens) or Decimal(0)
        cap = self._settings.openai_max_estimated_cost_usd_per_run
        if worst > cap:
            raise ProviderBudgetExceededError(
                PROVIDER,
                f"worst-case cost of one call (${worst}) exceeds the per-run AI budget (${cap})",
                details={"worst_case_usd": str(worst), "budget_usd": str(cap)},
            )

    async def complete(self, request: ChatRequest) -> ChatResult:
        self._ensure_callable("complete")
        self._precheck_budget(request)
        if not self._model_checked:
            await self.check_model()
        kwargs: dict[str, Any] = {
            "model": request.model or self.model,
            "input": [{"role": m.role, "content": m.content} for m in request.messages],
            "max_output_tokens": self._settings.openai_max_tokens,
            "store": False,
        }
        if request.response_schema is not None:
            kwargs["text"] = text_format(request.response_schema_name, request.response_schema)
        if self._settings.openai_reasoning_effort:
            kwargs["reasoning"] = {"effort": self._settings.openai_reasoning_effort}

        started = time.monotonic()
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._sdk().responses.create(**kwargs)
                break
            except openai.OpenAIError as exc:
                error = self._map(exc)
                retry = (
                    isinstance(error, RETRYABLE) and attempt <= self._settings.openai_max_retries
                )
                self._log(request, kwargs["model"], error.code, started, attempt, None, None)
                if not retry:
                    raise error from None
                await self._sleep(min(2.0**attempt, 16.0))
        return self._result(request, kwargs["model"], response, started, attempt)

    def _log(
        self,
        request: ChatRequest,
        model: str,
        status: str,
        started: float,
        attempt: int,
        usage: ChatUsage | None,
        cost: Decimal | None,
    ) -> None:
        logger.info(
            "openai call",
            extra={
                "provider": PROVIDER,
                "operation": request.purpose,
                "model": model,
                "status": status,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "attempt": attempt,
                "usage_total": usage.total_tokens if usage else None,  # "token" keys are redacted
                "estimated_cost_usd": str(cost) if cost is not None else None,
            },
        )

    def _result(
        self, request: ChatRequest, model: str, response: Any, started: float, attempts: int
    ) -> ChatResult:
        raw = getattr(response, "usage", None)
        usage = ChatUsage(
            prompt_tokens=int(getattr(raw, "input_tokens", 0) or 0),
            completion_tokens=int(getattr(raw, "output_tokens", 0) or 0),
            total_tokens=int(getattr(raw, "total_tokens", 0) or 0),
        )
        cost = self.cost(usage.prompt_tokens, usage.completion_tokens)
        spent = {"usage": usage.model_dump(), "cost_usd": str(cost) if cost is not None else None}
        answered = str(getattr(response, "model", "") or model)
        status = str(getattr(response, "status", "") or "")

        refusal = next(
            (
                str(getattr(part, "refusal", "") or "")
                for item in (getattr(response, "output", None) or [])
                for part in (getattr(item, "content", None) or [])
                if getattr(part, "type", None) == "refusal"
            ),
            None,
        )
        if refusal is not None:
            self._log(request, answered, "refused", started, attempts, usage, cost)
            raise ProviderRefusalError(
                PROVIDER, "the model refused to answer", details={**spent, "model": answered}
            )
        if status == "incomplete":
            reason = getattr(getattr(response, "incomplete_details", None), "reason", None)
            self._log(request, answered, "incomplete", started, attempts, usage, cost)
            raise ProviderIncompleteResponseError(
                PROVIDER,
                f"OpenAI response incomplete ({reason or 'unknown reason'})",
                details={**spent, "model": answered, "reason": reason},
            )
        text = str(getattr(response, "output_text", "") or "").strip()
        if status not in ("completed", "") or not text:
            self._log(request, answered, "malformed", started, attempts, usage, cost)
            raise ProviderMalformedResponseError(
                PROVIDER,
                "OpenAI returned no usable output text",
                details={**spent, "model": answered, "status": status},
            )
        self._log(request, answered, "ok", started, attempts, usage, cost)
        return ChatResult(
            provider=self.name,
            content=text,
            finish_reason="stop",
            model=answered,
            requested_model=model,
            gateway="openai",
            upstream_provider="openai",
            usage=usage,
            latency_ms=int((time.monotonic() - started) * 1000),
            attempts=attempts,
            cost_usd=cost,
        )

    async def review(self, request: LlmReviewRequest) -> LlmReviewResult:
        """AI review of a REVIEW case (owner decision 2026-10-13): attribution of funding news
        or a second opinion. One bounded call; the policy layer validates the answer."""
        self._ensure_callable("review")
        result = await self.complete(
            ChatRequest(
                messages=[ChatMessage(**m) for m in review_messages(request)],
                purpose=f"review_{request.task}",
                response_schema=REVIEW_SCHEMA,
                response_schema_name="lead_review",
            )
        )
        return parse_review(result.content, result.model)
