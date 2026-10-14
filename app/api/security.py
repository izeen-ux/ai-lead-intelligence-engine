"""API hardening (Phase 10): operator authentication, request-size limit, security headers.

Authorization model (deliberately simple - one operator role, no RBAC):
  * ``/health`` and ``/health/ready``: open, minimal, never contain secrets.
  * ``/dashboard`` (static page): open; it holds no data.
  * everything under ``/api/v1``: requires the operator token when ``OPERATOR_API_TOKEN`` is
    set (``Authorization: Bearer <token>``). The token is MANDATORY when APP_ENV is staging or
    production (the app refuses to start without it). Without a token (local development)
    only loopback clients are accepted.
  * every write (POST) additionally needs ``X-Dashboard-Request: 1`` (blocks cross-site forms).
"""

from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request, status
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.deps import get_app_settings
from app.core.config import Environment, Settings
from app.core.errors import ConfigurationError

LOCAL_CLIENTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})
MIN_TOKEN_LENGTH = 24


def enforce_production_profile(settings: Settings) -> None:
    """Fail fast: a staging/production app never starts with anonymous operator access."""
    token = settings.operator_api_token.get_secret_value() if settings.operator_api_token else ""
    if (
        settings.app_env in (Environment.STAGING, Environment.PRODUCTION)
        and len(token) < MIN_TOKEN_LENGTH
    ):
        raise ConfigurationError(
            f"APP_ENV={settings.app_env.value} requires OPERATOR_API_TOKEN "
            f"(at least {MIN_TOKEN_LENGTH} characters)"
        )
    if token and len(token) < MIN_TOKEN_LENGTH:
        raise ConfigurationError(
            f"OPERATOR_API_TOKEN must be at least {MIN_TOKEN_LENGTH} characters"
        )


def cors_origins(settings: Settings) -> list[str]:
    origins = [o.strip().rstrip("/") for o in settings.cors_allowed_origins.split(",") if o.strip()]
    for origin in origins:
        if "*" in origin or not origin.startswith(("http://", "https://")):
            raise ConfigurationError(
                "CORS_ALLOWED_ORIGINS must list exact http(s) origins (no wildcard)"
            )
    return origins


def _presented(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.headers.get("x-operator-token")


def require_operator(request: Request, settings: Settings = Depends(get_app_settings)) -> None:
    expected = settings.operator_api_token.get_secret_value() if settings.operator_api_token else ""
    if expected:
        presented = _presented(request) or ""
        if not hmac.compare_digest(presented.encode(), expected.encode()):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "operator token required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return
    client = request.client.host if request.client else ""
    if client not in LOCAL_CLIENTS:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "set OPERATOR_API_TOKEN to use the API from another machine",
        )


def require_write_header(request: Request) -> None:
    if request.headers.get("x-dashboard-request") != "1":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "missing X-Dashboard-Request header")


SECURITY_HEADERS = {
    b"x-content-type-options": b"nosniff",
    b"x-frame-options": b"DENY",
    b"referrer-policy": b"no-referrer",
    b"content-security-policy": (
        b"default-src 'self'; script-src 'self' 'unsafe-inline'; "
        b"style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; "
        b"frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
}


class HardeningMiddleware:
    """Pure ASGI: bounded request bodies (413) + security headers; API responses not cached."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body = max_body_bytes

    async def _reject(self, send: Send) -> None:
        body = b'{"error":{"code":"payload_too_large","message":"request body too large"}}'
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    *SECURITY_HEADERS.items(),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_body:
            await self._reject(send)
            return
        received = 0
        too_large = False

        async def limited_receive() -> Message:
            nonlocal received, too_large
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body:
                    too_large = True
                    return {"type": "http.disconnect"}
            return message

        is_api = str(scope.get("path", "")).startswith("/api/")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                extra = list(SECURITY_HEADERS.items())
                if is_api:
                    extra.append((b"cache-control", b"no-store"))
                present = {k.lower() for k, _ in message.get("headers", [])}
                message["headers"] = [
                    *message.get("headers", []),
                    *[(k, v) for k, v in extra if k not in present],
                ]
            if not too_large:
                await send(message)

        await self.app(scope, limited_receive, send_with_headers)
        if too_large:
            await self._reject(send)
