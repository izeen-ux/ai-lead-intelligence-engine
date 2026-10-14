"""HTTP client for the Techno-AI-Router (guide v1 + live behaviour verified 2026-10-01).

Verified live:
  GET  /health                                   {"status":"ok","checks":{"database":"up"},...}
  GET  /v1/providers?enabled=true                {"object","summary","data":[{provider,label,
                                                  modality,enabled,envKey,unit,official,modelCount}]}
  GET  /v1/models?provider=openrouter&modality=llm
                                                 {"object","summary","data":[{provider,modality,
                                                  model,price:"$0 / $0",unit,enabled,official,tags,
                                                  inputs,outputs}]}
  POST /v1/chat/completions without a key        401 {"message":"Missing API key",
                                                      "error":"Unauthorized","statusCode":401}
Documented (guide): chat body {gateway, model, messages, fallback[], temperature, maxTokens};
response {model, choices[0].message.content, choices[0].finish_reason, usage{prompt_tokens,
completion_tokens,total_tokens}, x_router{gateway,provider}}; 400 / 401 / 403 (key scope) /
429 / 5xx. The key is sent only as ``Authorization: Bearer`` and never logged.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

import httpx2

from app.core.config import Settings
from app.core.errors import (
    ProviderError,
    ProviderMalformedResponseError,
    ProviderModelUnavailableError,
    ProviderNotConfiguredError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.providers.contracts import RouterModel

NAME = "ai_router"
_PRICE = re.compile(r"\$\s*([\d.]+)\s*/\s*\$\s*([\d.]+)")
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
Sleep = Callable[[float], Awaitable[None]]


def base_url_problem(settings: Settings) -> str | None:
    url = settings.ai_router_base_url
    if not url:
        return "AI_ROUTER_BASE_URL not set (e.g. https://techno-ai-router.vercel.app)"
    parts = urlsplit(url)
    if parts.scheme != "https" and (parts.hostname or "") not in _LOOPBACK:
        return "AI_ROUTER_BASE_URL must use https (the API key travels in a header)"
    return None


def parse_price(price: str | None) -> tuple[Decimal | None, Decimal | None]:
    m = _PRICE.search(price or "")
    if not m:
        return None, None
    try:
        return Decimal(m.group(1)), Decimal(m.group(2))
    except InvalidOperation:  # pragma: no cover - regex guarantees digits
        return None, None


def parse_model(raw: Any) -> RouterModel | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("model"), str):
        return None
    p_in, p_out = parse_price(raw.get("price") if isinstance(raw.get("price"), str) else None)
    raw_tags = raw.get("tags")
    tags: list[Any] = raw_tags if isinstance(raw_tags, list) else []
    return RouterModel(
        provider=str(raw.get("provider") or ""),
        modality=str(raw.get("modality") or ""),
        model=raw["model"],
        price=raw.get("price") if isinstance(raw.get("price"), str) else None,
        price_in_per_1m_usd=p_in,
        price_out_per_1m_usd=p_out,
        enabled=raw.get("enabled") is True,
        tags=[str(t) for t in tags],
    )


def _detail(resp: httpx2.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        msg = body.get("message") or body.get("error") or ""
        if isinstance(msg, dict):
            msg = msg.get("message", "")
        return str(msg)[:300]
    return ""


class RouterClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._sleep = sleep

    def _client(self, *, auth: bool) -> httpx2.AsyncClient:
        headers = {"Content-Type": "application/json"}
        key = self._settings.ai_router_api_key
        if auth and key is not None and key.get_secret_value():
            headers["Authorization"] = f"Bearer {key.get_secret_value()}"
        return httpx2.AsyncClient(
            base_url=str(self._settings.ai_router_base_url).rstrip("/"),
            headers=headers,
            timeout=self._settings.ai_router_timeout_seconds,
            transport=self._transport,
        )

    async def _send(
        self, method: str, path: str, *, params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None, auth: bool = False,
    ) -> dict[str, Any]:  # fmt: skip
        problem = base_url_problem(self._settings)
        if problem is not None:
            raise ProviderNotConfiguredError(NAME, problem)
        try:
            async with self._client(auth=auth) as client:
                resp = await client.request(method, path, params=params, json=body)
        except httpx2.TimeoutException:
            raise ProviderTimeoutError(
                NAME, "AI router did not answer in time", details={"path": path}
            ) from None
        except httpx2.TransportError as exc:
            raise ProviderUnavailableError(
                NAME, f"AI router unreachable ({type(exc).__name__})", details={"path": path}
            ) from None
        self._raise_for_status(resp, path)
        try:
            data = resp.json()
        except ValueError:
            raise ProviderMalformedResponseError(NAME, "router response is not JSON") from None
        if not isinstance(data, dict):
            raise ProviderMalformedResponseError(NAME, "router response is not an object")
        return data

    @staticmethod
    def _raise_for_status(resp: httpx2.Response, path: str) -> None:
        status = resp.status_code
        if status < 400:
            return
        detail = _detail(resp)
        details = {"http_status": status, "path": path}
        if status == 401:
            raise ProviderNotConfiguredError(
                NAME, f"router rejected the API key (401): {detail}", details=details
            )
        if status == 403:
            raise ProviderNotConfiguredError(
                NAME,
                f"API key not allowed for this provider/model (403): {detail}",
                details=details,
            )
        if status == 404 or (status == 400 and "model" in detail.lower()):
            raise ProviderModelUnavailableError(
                NAME, f"model unavailable ({status}): {detail}", details=details
            )
        if status == 429:
            raise ProviderRateLimitedError(
                NAME, f"router rate limit (429): {detail}", details=details
            )
        if status >= 500:
            raise ProviderUnavailableError(
                NAME, f"router/upstream error ({status}): {detail}", details=details
            )
        raise ProviderError(
            NAME, f"router refused the request ({status}): {detail}", details=details
        )

    # ------------------------------------------------------------------ discovery (no key)
    async def health(self) -> dict[str, Any]:
        return await self._send("GET", "/health")

    async def providers(self, *, enabled_only: bool = True) -> list[dict[str, Any]]:
        data = await self._send(
            "GET", "/v1/providers", params={"enabled": "true"} if enabled_only else None
        )
        items = data.get("data")
        if not isinstance(items, list):
            raise ProviderMalformedResponseError(NAME, "providers response has no 'data' list")
        return [p for p in items if isinstance(p, dict)]

    async def models(self, *, provider: str, modality: str = "llm") -> list[RouterModel]:
        data = await self._send(
            "GET", "/v1/models", params={"provider": provider, "modality": modality}
        )
        items = data.get("data")
        if not isinstance(items, list):
            raise ProviderMalformedResponseError(NAME, "models response has no 'data' list")
        return [m for m in (parse_model(i) for i in items) if m is not None]

    # ------------------------------------------------------------------ chat (key)
    async def chat(self, body: dict[str, Any], *, retries: int) -> tuple[dict[str, Any], int]:
        """POST /v1/chat/completions with bounded retry on 429 / 5xx / timeout.
        Returns (response, attempts)."""
        attempt = 0
        while True:
            attempt += 1
            try:
                return await self._send(
                    "POST", "/v1/chat/completions", body=body, auth=True
                ), attempt
            except (ProviderRateLimitedError, ProviderUnavailableError, ProviderTimeoutError):
                if attempt > retries:
                    raise
                await self._sleep(min(2.0**attempt, 8.0))
