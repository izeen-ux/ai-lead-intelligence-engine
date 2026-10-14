"""Techno-AI-Router adapter: discovery, chat contract, model policy, fallback, retry, errors.
httpx2.MockTransport only - never the network."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx2
import pytest

from app.core.errors import (
    ProviderCallBlockedError,
    ProviderError,
    ProviderMalformedResponseError,
    ProviderModelUnavailableError,
    ProviderNotConfiguredError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.providers.ai_router import AiRouterAdapter
from app.providers.ai_router.client import parse_price
from app.providers.contracts import ChatMessage, ChatRequest

KEY = "tair_test_key_value_never_logged_0123456789"
URL = "https://router.example"


def model(name: str, price: str = "$0 / $0", enabled: bool = True, tags: list[str] | None = None,
          provider: str = "openrouter") -> dict[str, Any]:  # fmt: skip
    return {"provider": provider, "modality": "llm", "model": name, "price": price,
            "unit": "per 1M tokens (in / out)", "enabled": enabled, "official": False,
            "tags": tags or ["free"], "inputs": ["text"], "outputs": ["text"]}  # fmt: skip


CATALOG = [
    model("google/gemma-4-31b-it:free", tags=["free", "recommended"]),
    model("openai/gpt-oss-20b:free"),
    model("nvidia/nemotron-nano-9b-v2:free"),
    model("poolside/laguna-xs-2.1:free"),
    model("openai/gpt-4.1", price="$2 / $8", tags=["best"]),
    model("disabled/free:free", enabled=False),
]


def completion(content: str = "OK", model_id: str = "google/gemma-4-31b-it:free") -> dict[str, Any]:
    return {
        "object": "chat.completion",
        "model": model_id,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 18, "completion_tokens": 4, "total_tokens": 22},
        "x_router": {"gateway": "openrouter", "provider": "openrouter"},
    }


class Router:
    """Fake router: discovery + scripted chat responses (one per call, last one repeats)."""

    def __init__(self, chat: list[Any] | None = None, catalog: list[dict[str, Any]] | None = None):
        self.chat = chat or [(200, completion())]
        self.catalog = CATALOG if catalog is None else catalog
        self.requests: list[httpx2.Request] = []

    def __call__(self, req: httpx2.Request) -> httpx2.Response:
        self.requests.append(req)
        path = req.url.path
        if path == "/health":
            return httpx2.Response(200, json={"status": "ok", "checks": {"database": "up"}})
        if path == "/v1/providers":
            return httpx2.Response(
                200,
                json={
                    "object": "list",
                    "data": [{"provider": "openrouter", "modality": "llm", "enabled": True}],
                },
            )
        if path == "/v1/models":
            return httpx2.Response(
                200, json={"object": "list", "summary": {}, "data": self.catalog}
            )
        step = self.chat.pop(0) if len(self.chat) > 1 else self.chat[0]
        if isinstance(step, Exception):
            raise step
        status, body = step
        return (
            httpx2.Response(status, json=body)
            if not isinstance(body, str)
            else httpx2.Response(status, text=body)
        )

    def chats(self) -> list[dict[str, Any]]:
        return [
            json.loads(r.content) for r in self.requests if r.url.path == "/v1/chat/completions"
        ]


def adapter(make_settings, router: Router, **kw: Any) -> AiRouterAdapter:
    base: dict[str, Any] = {
        "provider_mode": "live", "allow_paid_provider_calls": True, "ai_router_api_key": KEY,
        "ai_router_base_url": URL, "ai_router_max_retries": 1,
    }  # fmt: skip
    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)

    a = AiRouterAdapter(
        make_settings(**(base | kw)), transport=httpx2.MockTransport(router), sleep=sleep
    )
    a.sleeps = sleeps  # type: ignore[attr-defined]
    return a


REQ = ChatRequest(messages=[ChatMessage(role="user", content="Say hello")])


# ================================================================== discovery
async def test_discovery_needs_no_key_and_parses_the_live_shape(make_settings) -> None:
    router = Router()
    a = adapter(
        make_settings,
        router,
        ai_router_api_key=None,
        provider_mode="mock",
        allow_paid_provider_calls=False,
    )
    assert (await a.health())["status"] == "ok"
    assert (await a.list_providers())[0]["provider"] == "openrouter"
    models = await a.list_models()
    assert len(models) == 6
    assert models[0].is_free and models[0].tags == ["free", "recommended"]
    assert models[4].price_in_per_1m_usd == Decimal(2) and not models[4].is_free
    assert all("Authorization" not in r.headers for r in router.requests)
    assert router.requests[2].url.params["provider"] == "openrouter"
    assert router.requests[2].url.params["modality"] == "llm"


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        ("$0 / $0", (Decimal(0), Decimal(0))),
        ("$0.15 / $0.6", (Decimal("0.15"), Decimal("0.6"))),
        ("free", (None, None)),
        (None, (None, None)),
    ],
)
def test_price_parsing(price, expected) -> None:
    assert parse_price(price) == expected


# ================================================================== chat contract
async def test_chat_request_and_response_contract(make_settings) -> None:
    router = Router()
    result = await adapter(make_settings, router).complete(REQ)
    [req] = [r for r in router.requests if r.url.path == "/v1/chat/completions"]
    assert req.headers["Authorization"] == f"Bearer {KEY}"
    body = router.chats()[0]
    assert body["gateway"] == "openrouter"
    assert body["model"] == "google/gemma-4-31b-it:free"  # recommended first when none configured
    assert body["maxTokens"] == 1500 and "max_tokens" not in body  # router reads camelCase
    assert body["temperature"] == 0.2
    assert body["fallback"] == [
        "openrouter/openai/gpt-oss-20b:free",
        "openrouter/nvidia/nemotron-nano-9b-v2:free",
    ]
    assert body["messages"] == [{"role": "user", "content": "Say hello"}]
    assert (result.content, result.finish_reason, result.model) == (
        "OK",
        "stop",
        "google/gemma-4-31b-it:free",
    )
    assert (result.gateway, result.upstream_provider) == ("openrouter", "openrouter")
    assert (
        result.usage.prompt_tokens,
        result.usage.completion_tokens,
        result.usage.total_tokens,
    ) == (18, 4, 22)
    assert result.cost_usd == Decimal(0) and result.attempts == 1


async def test_configured_model_and_fallbacks_are_used(make_settings) -> None:
    router = Router()
    await adapter(
        make_settings, router, ai_router_default_model="openai/gpt-oss-20b:free",
        ai_router_fallback_models=(
            "openrouter/poolside/laguna-xs-2.1:free, groq/llama-3.1-8b-instant"
        ),
    ).complete(REQ)  # fmt: skip
    body = router.chats()[0]
    assert body["model"] == "openai/gpt-oss-20b:free"
    assert body["fallback"] == ["openrouter/poolside/laguna-xs-2.1:free"]  # other gateway dropped


async def test_unavailable_primary_promotes_an_allowed_fallback(make_settings) -> None:
    router = Router()
    a = adapter(make_settings, router, ai_router_default_model="disabled/free:free",
                ai_router_fallback_models="openrouter/openai/gpt-oss-20b:free")  # fmt: skip
    await a.complete(REQ)
    assert router.chats()[0]["model"] == "openai/gpt-oss-20b:free"


async def test_cost_ceiling_excludes_paid_models(make_settings) -> None:
    router = Router()
    a = adapter(make_settings, router, ai_router_default_model="openai/gpt-4.1")
    with pytest.raises(ProviderModelUnavailableError, match="cost ceiling"):
        await a.complete(REQ)
    assert router.chats() == []  # refused before any chat call
    paid_ok = adapter(make_settings, Router(), ai_router_default_model="openai/gpt-4.1",
                      ai_router_max_price_per_1m_tokens_usd=Decimal(10))  # fmt: skip
    priced = await paid_ok.complete(REQ)
    assert priced.cost_usd is not None


async def test_router_reported_model_unavailable_falls_back_client_side(make_settings) -> None:
    router = Router(
        chat=[
            (404, {"message": "model not found"}),
            (200, completion(model_id="openai/gpt-oss-20b:free")),
        ]
    )
    result = await adapter(make_settings, router).complete(REQ)
    models = [b["model"] for b in router.chats()]
    assert models == ["google/gemma-4-31b-it:free", "openai/gpt-oss-20b:free"]
    assert result.model == "openai/gpt-oss-20b:free" and result.attempts == 2


async def test_all_models_unavailable(make_settings) -> None:
    router = Router(chat=[(400, {"message": "Unknown model"})])
    with pytest.raises(ProviderModelUnavailableError):
        await adapter(make_settings, router).complete(REQ)
    assert len(router.chats()) == 3


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (429, ProviderRateLimitedError),
        (500, ProviderUnavailableError),
        (503, ProviderUnavailableError),
    ],
)
async def test_retryable_errors_retry_then_raise(make_settings, status, error) -> None:
    router = Router(chat=[(status, {"message": "busy"})])
    a = adapter(make_settings, router)
    with pytest.raises(error):
        await a.complete(REQ)
    assert len(router.chats()) == 2  # 1 try + 1 retry
    assert a.sleeps == [2.0]  # type: ignore[attr-defined]


async def test_retry_then_success(make_settings) -> None:
    router = Router(chat=[(429, {"message": "slow down"}), (200, completion())])
    result = await adapter(make_settings, router).complete(REQ)
    assert result.attempts == 2


@pytest.mark.parametrize(
    ("status", "error"),
    [(401, ProviderNotConfiguredError), (403, ProviderNotConfiguredError), (400, ProviderError)],
)
async def test_non_retryable_4xx(make_settings, status, error) -> None:
    router = Router(
        chat=[(status, {"message": "bad field", "error": "Bad Request", "statusCode": status})]
    )
    with pytest.raises(error) as exc:
        await adapter(make_settings, router).complete(REQ)
    assert KEY not in str(exc.value) and KEY not in str(exc.value.details)
    assert len(router.chats()) == 1


async def test_timeout_is_typed(make_settings) -> None:
    router = Router(chat=[httpx2.ReadTimeout("slow")])
    with pytest.raises(ProviderTimeoutError):
        await adapter(make_settings, router, ai_router_max_retries=0).complete(REQ)


@pytest.mark.parametrize(
    "body",
    [
        {"choices": []},
        {"choices": [{"message": {"content": ""}}]},
        {"no": "choices"},
        "<html>",
        ["x"],
    ],
)
async def test_malformed_responses(make_settings, body) -> None:
    with pytest.raises(ProviderMalformedResponseError):
        await adapter(make_settings, Router(chat=[(200, body)])).complete(REQ)


# ================================================================== guards / config
async def test_paid_guard_and_missing_key_block_before_any_request(make_settings) -> None:
    router = Router()
    with pytest.raises(ProviderCallBlockedError):
        await adapter(make_settings, router, allow_paid_provider_calls=False).complete(REQ)
    with pytest.raises(ProviderNotConfiguredError):
        await adapter(make_settings, router, ai_router_api_key=None).complete(REQ)
    assert router.requests == []


def test_base_url_rules(make_settings) -> None:
    assert (
        make_settings(ai_router_base_url="").ai_router_base_url
        == "https://techno-ai-router.vercel.app"
    )
    insecure = adapter(make_settings, Router(), ai_router_base_url="http://router.example")
    assert insecure.status().operational is False
    assert "https" in (insecure.status().detail or "")
    assert (
        adapter(make_settings, Router(), ai_router_base_url="http://127.0.0.1:9000")
        .status()
        .operational
    )
    assert (
        adapter(make_settings, Router(), ai_router_api_key=None).status().detail
        == "AI_ROUTER_API_KEY not set"
    )
