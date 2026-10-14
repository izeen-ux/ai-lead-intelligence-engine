"""Service-account access tokens for the Sheets API (the approach validated pre-Phase-5).

The key FILE is read by ``google-auth`` only; its contents never enter settings, logs, errors
or reports. Only the service-account email (needed to share the Sheet) is ever shown.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import time
from pathlib import Path
from typing import Any, Protocol

from app.core.errors import ProviderNotConfiguredError

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
PROVIDER = "google_sheets"


class TokenProvider(Protocol):
    async def token(self) -> str: ...

    @property
    def identity(self) -> str | None: ...


def service_account_email(path: Path) -> str | None:
    """Read ONLY the public ``client_email`` field (for setup messages). Never raises."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    email = data.get("client_email") if isinstance(data, dict) else None
    return email if isinstance(email, str) else None


class ServiceAccountTokenProvider:
    """Cached OAuth access token from a service-account key file (google-auth)."""

    def __init__(self, key_path: Path) -> None:
        self._path = key_path
        self._creds: Any = None
        self._expires = 0.0

    @property
    def identity(self) -> str | None:
        return service_account_email(self._path)

    def _refresh(self) -> str:
        if not self._path.is_file():
            raise ProviderNotConfiguredError(
                PROVIDER,
                "service-account key file not found (GOOGLE_SERVICE_ACCOUNT_JSON)",
                details={"path": str(self._path)},
            )
        try:
            sa = importlib.import_module("google.oauth2.service_account")
            transport = importlib.import_module("google.auth.transport.requests")
            if self._creds is None:
                self._creds = sa.Credentials.from_service_account_file(
                    str(self._path), scopes=[SHEETS_SCOPE]
                )
            self._creds.refresh(transport.Request())
        except ProviderNotConfiguredError:
            raise
        except Exception as exc:  # never echo library messages: they may quote the key file
            raise ProviderNotConfiguredError(
                PROVIDER,
                "could not obtain a Google access token from the service-account key",
                details={"error_type": type(exc).__name__},
            ) from None
        self._expires = time.monotonic() + 45 * 60  # tokens live 60 min; refresh early
        return str(self._creds.token)

    async def token(self) -> str:
        if self._creds is not None and self._creds.token and time.monotonic() < self._expires:
            return str(self._creds.token)
        return await asyncio.to_thread(self._refresh)


class StaticTokenProvider:
    """Tests only: a fixed token, no Google library involved."""

    def __init__(self, token: str, identity: str | None = None) -> None:
        self._token = token
        self._identity = identity

    @property
    def identity(self) -> str | None:
        return self._identity

    async def token(self) -> str:
        return self._token
