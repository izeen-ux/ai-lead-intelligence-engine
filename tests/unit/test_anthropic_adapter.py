"""Claude adapter over a fake SDK client (no network): request shape, structured output,
refusal / truncation / error mapping, budget guard, the AI review path, provider selection."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest

from app.core.errors import (
    ProviderBudgetExceededError,
    ProviderCallBlockedError,
    ProviderIncompleteResponseError,
    ProviderMalformedResponseError,
    ProviderNotConfiguredError,
    ProviderQuotaExhaustedError,
    ProviderRateLimitedError,
    ProviderRefusalError,
)
from app.domain.analysis.config import AnalysisConfig
from app.providers.anthropic import AnthropicAdapter
from app.providers.contracts import ChatMessage, ChatRequest, LlmReviewRequest
from app.providers.registry import ProviderRegistry, live_reasoning

KEY = "sk-ant-api03-TESTKEY000000000000"
REQ = ChatRequest(
    messages=[
        ChatMessage(role="system", content="You analyse leads."),
        ChatMessage(role="user", content="Say hello"),
    ],
    response_schema={
        "type": "object",
        "properties": {"greeting": {"type": "string"}},
        "required": ["greeting"],
        "additionalProperties": False,
    },
)


def message(text: str = '{"greeting": "hi"}', **over: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "model": "claude-opus-5-5",
        "stop_reason": "end_turn",
        "stop_details": None,
        "content": [
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
        "usage": SimpleNamespace(
            input_tokens=1000, output_tokens=200, cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    }  # fmt: skip
    return SimpleNamespace(**(base | over))


class FakeClaude:
    def __init__(self, answer: Any = None) -> None:
        self.answer = answer if answer is not None else message()
        self.calls: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))
        self.models = SimpleNamespace(retrieve=self._retrieve)

    async def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    async def _retrieve(self, model: str) -> Any:
        return SimpleNamespace(id=model, display_name="Claude")


def adapter(make_settings, fake: FakeClaude, **kw: Any) -> AnthropicAdapter:
    settings = make_settings(
        provider_mode="live", allow_paid_provider_calls=True, anthropic_api_key=KEY,
        reasoning_provider="anthropic", **kw,
    )  # fmt: skip
    return AnthropicAdapter(settings, client=fake)


def status_error(cls: type, status: int, kind: str) -> Exception:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    body = {"type": "error", "error": {"type": kind, "message": "no"}}
    error: Exception = cls(
        "no", response=httpx2.Response(status, request=request, json=body), body=body
    )
    return error


async def test_request_shape_and_result(make_settings) -> None:
    fake = FakeClaude()
    result = await adapter(make_settings, fake).complete(REQ)
    (call,) = fake.calls
    assert call["model"] == "claude-opus-5-5" and call["max_tokens"] == 16000
    assert call["system"] == "You analyse leads."
    assert call["messages"] == [{"role": "user", "content": "Say hello"}]
    assert call["output_config"]["effort"] == "medium"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert "thinking" not in call and "temperature" not in call  # adaptive thinking, no sampling
    # a policy refusal is retried server-side on Anthropic's recommended fallback model
    assert (call["betas"], call["fallbacks"]) == (["server-side-fallback-2026-07-01"], "default")
    assert json.loads(result.content) == {"greeting": "hi"}
    assert (result.gateway, result.provider.value) == ("anthropic", "anthropic")
    assert result.usage.total_tokens == 1200
    assert result.cost_usd == Decimal("0.008000")  # 1000 * $4/M + 200 * $20/M


async def test_other_models_and_switches(make_settings) -> None:
    fake = FakeClaude(message(model="claude-haiku-5-5"))
    a = adapter(make_settings, fake, anthropic_model="claude-haiku-5-5", anthropic_effort="low")
    result = await a.complete(ChatRequest(messages=[ChatMessage(role="user", content="x")]))
    (call,) = fake.calls
    assert "fallbacks" not in call and "betas" not in call  # not offered for this model
    assert "format" not in call["output_config"] and call["output_config"]["effort"] == "low"
    assert "system" not in call and result.cost_usd == Decimal("0.000200")
    off = FakeClaude()
    await adapter(make_settings, off, anthropic_refusal_fallback=False).complete(REQ)
    assert "fallbacks" not in off.calls[0]


async def test_refusal_and_truncation_are_typed_errors(make_settings) -> None:
    refused = message(stop_reason="refusal", stop_details=SimpleNamespace(category="cyber"))
    with pytest.raises(ProviderRefusalError) as exc:
        await adapter(make_settings, FakeClaude(refused)).complete(REQ)
    assert exc.value.details["category"] == "cyber"
    with pytest.raises(ProviderIncompleteResponseError):
        await adapter(make_settings, FakeClaude(message(stop_reason="max_tokens"))).complete(REQ)
    with pytest.raises(ProviderMalformedResponseError):
        await adapter(make_settings, FakeClaude(message(content=[]))).complete(REQ)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (status_error(anthropic.AuthenticationError, 401, "authentication_error"),
         ProviderNotConfiguredError),
        (status_error(anthropic.APIStatusError, 402, "billing_error"), ProviderQuotaExhaustedError),
        (status_error(anthropic.RateLimitError, 429, "rate_limit_error"), ProviderRateLimitedError),
    ],
)  # fmt: skip
async def test_api_errors_are_mapped_without_the_key(make_settings, error, expected) -> None:
    with pytest.raises(expected) as exc:
        await adapter(make_settings, FakeClaude(error)).complete(REQ)
    assert KEY not in exc.value.message and KEY not in json.dumps(exc.value.details)


async def test_guards_run_before_any_call(make_settings) -> None:
    fake = FakeClaude()
    cheap = {"anthropic_max_estimated_cost_usd_per_run": Decimal("0.01")}
    with pytest.raises(ProviderBudgetExceededError):
        await adapter(make_settings, fake, **cheap).complete(REQ)
    blocked = AnthropicAdapter(
        make_settings(provider_mode="live", anthropic_api_key=KEY), client=fake
    )
    with pytest.raises(ProviderCallBlockedError):  # paid switch off
        await blocked.complete(REQ)
    assert fake.calls == []


async def test_review_uses_one_structured_call(make_settings) -> None:
    answer = {
        "decision": "qualified", "confidence": 0.9, "reasons": ["same sneaker brand"],
        "supporting_evidence_ids": ["evt_1"],
    }  # fmt: skip
    fake = FakeClaude(message(json.dumps(answer)))
    result = await adapter(make_settings, fake).review(
        LlmReviewRequest(
            subject_id=uuid.uuid4(),
            bundle={"company": "Zaydn", "events": [{"event_id": "evt_1"}]},
            allowed_evidence_ids=["evt_1"],
            qualified_allowed=True,
            task="attribution",
            question_event_ids=["evt_1"],
        )
    )
    assert (result.provider.value, result.decision.value) == ("anthropic", "qualified")
    assert "TASK: attribution" in fake.calls[0]["messages"][0]["content"]


def test_reasoning_provider_setting_selects_claude(make_settings) -> None:
    settings = make_settings(
        provider_mode="live", reasoning_provider="anthropic", anthropic_api_key=KEY
    )
    assert isinstance(live_reasoning(settings), AnthropicAdapter)
    assert isinstance(ProviderRegistry(settings).reasoning, AnthropicAdapter)
    config = AnalysisConfig.from_settings(settings)
    assert (config.model.gateway, config.model.primary_model) == ("anthropic", "claude-opus-5-5")
    missing = {p.value for p in settings.missing_provider_credentials()}
    assert "openai" not in missing and "anthropic" not in missing  # only the selected one counts
    status = AnthropicAdapter(settings).status()
    assert status.configured and status.operational and "claude-opus-5-5" in (status.detail or "")
