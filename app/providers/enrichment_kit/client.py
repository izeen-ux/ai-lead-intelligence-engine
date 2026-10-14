"""HTTP client for the project's enrichment sidecar (``sidecars/enrichment-kit``).

The sidecar is the ONLY process that talks to Hunter and Apollo (it holds their keys). This
client refuses non-loopback URLs, sends the local bearer token, maps transport/HTTP failures to
typed errors and never logs request bodies. Contract (sidecar lib.mjs):

  GET  /health        -> {status, enrichmentKit:{version, commit}, providers:{hunter:{configured},
                          apollo:{configured}}, auth, providerCalls:{used, limit}}
  POST /v1/enrich     {input, providers, minConfidence, earlyExitOnConfidence}
                      -> {success, best, outputs:[{provider, output, error, errorKind, statusCode,
                          latencyMs, called}], totalCost, order}
  POST /v1/verify     {email} -> {provider, email, status, score, errorKind, httpStatus}
  POST /v1/people     {domain, limit} -> {provider, people:[...], errorKind, httpStatus}
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx2

from app.core.config import Settings
from app.core.errors import (
    ProviderError,
    ProviderMalformedResponseError,
    ProviderNotConfiguredError,
    ProviderPolicyBlockedError,
    ProviderQuotaExhaustedError,
    ProviderRateLimitedError,
    ProviderRunFailedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def endpoint_problem(settings: Settings) -> str | None:
    url = settings.enrichment_kit_base_url
    if not url:
        return "ENRICHMENT_KIT_BASE_URL not set (local enrichment sidecar)"
    host = (urlsplit(url).hostname or "").lower()
    if not settings.is_private_service_host(host):
        return (
            "ENRICHMENT_KIT_BASE_URL must be a loopback address or a host listed in "
            f"INTERNAL_SERVICE_HOSTS (got host {host!r})"
        )
    return None


def error_for_kind(provider: str, kind: str, message: str) -> ProviderError:
    """Typed error for an account-level failure reported by the sidecar."""
    details = {"error_kind": kind}
    if kind == "auth":
        return ProviderNotConfiguredError(
            provider, f"{provider} rejected the API key", details=details
        )
    if kind == "forbidden":
        return ProviderNotConfiguredError(
            provider, f"{provider}: endpoint not allowed for this plan/key", details=details
        )
    if kind == "quota_exhausted":
        return ProviderQuotaExhaustedError(
            provider, f"{provider} usage limit reached", details=details
        )
    if kind == "rate_limited":
        return ProviderRateLimitedError(provider, f"{provider} rate limit", details=details)
    if kind == "timeout":
        return ProviderTimeoutError(provider, f"{provider} timed out", details=details)
    if kind == "pii_blocked":
        return ProviderPolicyBlockedError(
            provider, f"{provider} refused (PII opt-out)", details=details
        )
    if kind == "budget_exhausted":
        return ProviderQuotaExhaustedError(
            provider, "sidecar provider-call limit reached", details=details
        )
    return ProviderUnavailableError(provider, f"{provider}: {message or kind}", details=details)


class SidecarClient:
    def __init__(
        self,
        settings: Settings,
        provider: str,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._provider = provider  # name used in typed errors
        self._transport = transport

    def _client(self) -> httpx2.AsyncClient:
        headers = {}
        token = self._settings.enrichment_kit_api_token
        if token is not None and token.get_secret_value():
            headers["Authorization"] = f"Bearer {token.get_secret_value()}"
        return httpx2.AsyncClient(
            base_url=str(self._settings.enrichment_kit_base_url).rstrip("/"),
            headers=headers,
            timeout=self._settings.enrichment_kit_timeout_seconds,
            transport=self._transport,
        )

    async def get(self, path: str) -> dict[str, Any]:
        return await self._send("GET", path, None)

    async def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._send("POST", path, payload)

    async def _send(self, method: str, path: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        name = self._provider
        try:
            async with self._client() as client:
                resp = await client.request(method, path, json=payload)
        except httpx2.TimeoutException:
            raise ProviderTimeoutError(
                name, "enrichment sidecar did not answer in time", details={"path": path}
            ) from None
        except httpx2.TransportError as exc:
            raise ProviderUnavailableError(
                name,
                f"enrichment sidecar unreachable ({type(exc).__name__}); is it running?",
                details={"path": path},
            ) from None
        try:
            body = resp.json()
        except ValueError:
            raise ProviderMalformedResponseError(name, "sidecar response is not JSON") from None
        if not isinstance(body, dict):
            raise ProviderMalformedResponseError(name, "sidecar response is not an object")
        status = resp.status_code
        if status == 401:
            raise ProviderNotConfiguredError(
                name, "sidecar rejected ENRICHMENT_KIT_API_TOKEN", details={"http_status": 401}
            )
        if status == 409:
            raise ProviderNotConfiguredError(
                name,
                "no requested provider is configured in the sidecar (.env keys?)",
                details={"http_status": 409},
            )
        if status in (400, 413, 422):
            raise ProviderError(
                name, f"sidecar refused the request ({status})", details={"http_status": status}
            )
        if status >= 400:
            raise ProviderRunFailedError(
                name, f"sidecar error {status}", details={"http_status": status}
            )
        return body
