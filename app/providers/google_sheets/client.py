"""Google Sheets API v4 writer (live) behind the ``SheetsWriter`` capability.

Endpoints (https://sheets.googleapis.com/v4/spreadsheets/{id}):
  GET  ?fields=properties.title,sheets.properties.title     workbook identity + tabs
  POST :batchUpdate {addSheet}                                create a worksheet
  GET  /values/'Tab'!A1:<col>                                 read a tab
  POST /values:batchUpdate {valueInputOption: RAW, data}      write rows (one call per batch)
Errors: 401/403 -> not configured / no access (share the Sheet with the service account),
404 -> spreadsheet not found, 429/5xx/timeout -> bounded retry, other 4xx -> hard failure.
Values are written RAW: nothing typed into a cell is ever evaluated as a formula.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx2

from app.core.errors import (
    ProviderError,
    ProviderMalformedResponseError,
    ProviderNotConfiguredError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.providers.google_sheets.auth import PROVIDER, TokenProvider

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
RETRYABLE = (ProviderRateLimitedError, ProviderUnavailableError, ProviderTimeoutError)


@dataclass(frozen=True)
class SpreadsheetInfo:
    title: str
    worksheets: list[str]


def column_letter(n: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA."""
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def a1(worksheet: str, cell: str) -> str:
    return "'" + worksheet.replace("'", "''") + "'!" + cell


def _message(resp: httpx2.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    return str(error.get("message", ""))[:300] if isinstance(error, dict) else ""


class GoogleSheetsWriter:
    """Live ``SheetsWriter``. Every request carries a fresh-enough bearer token."""

    mode = "live"

    def __init__(
        self,
        spreadsheet_id: str,
        tokens: TokenProvider,
        *,
        timeout: float = 30.0,
        max_retries: int = 2,
        transport: httpx2.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        base_url: str = SHEETS_API,
    ) -> None:
        if not spreadsheet_id.strip():
            raise ProviderNotConfiguredError(PROVIDER, "GOOGLE_SHEETS_SPREADSHEET_ID is not set")
        self._base = f"{base_url}/{spreadsheet_id.strip()}"
        self._tokens = tokens
        self._timeout = timeout
        self._retries = max_retries
        self._transport = transport
        self._sleep = sleep
        self.api_calls = 0

    @property
    def identity(self) -> str | None:
        return self._tokens.identity

    async def _send(self, method: str, url: str, **kw: Any) -> dict[str, Any]:
        attempt = 0
        while True:
            attempt += 1
            try:
                return await self._once(method, url, **kw)
            except RETRYABLE:
                if attempt > self._retries:
                    raise
                await self._sleep(min(2.0**attempt, 16.0))

    async def _once(self, method: str, url: str, **kw: Any) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {await self._tokens.token()}"}
        self.api_calls += 1
        started = time.monotonic()
        try:
            async with httpx2.AsyncClient(
                timeout=self._timeout, transport=self._transport, headers=headers
            ) as client:
                resp = await client.request(method, url, **kw)
        except httpx2.TimeoutException:
            raise ProviderTimeoutError(PROVIDER, "Google Sheets request timed out") from None
        except httpx2.TransportError as exc:
            raise ProviderUnavailableError(
                PROVIDER, "Google Sheets unreachable", details={"error_type": type(exc).__name__}
            ) from None
        details = {"status": resp.status_code, "ms": int((time.monotonic() - started) * 1000)}
        status = resp.status_code
        if status in (401, 403):
            raise ProviderNotConfiguredError(
                PROVIDER,
                f"Google Sheets refused access (HTTP {status}): share the spreadsheet with the "
                "service-account email as Editor",
                details={**details, "detail": _message(resp)},
            )
        if status == 404:
            raise ProviderNotConfiguredError(
                PROVIDER,
                "spreadsheet not found - check GOOGLE_SHEETS_SPREADSHEET_ID",
                details=details,
            )
        if status == 429:
            raise ProviderRateLimitedError(PROVIDER, "Google Sheets rate limit", details=details)
        if status >= 500:
            raise ProviderUnavailableError(
                PROVIDER, f"Google Sheets server error {status}", details=details
            )
        if status >= 400:
            raise ProviderError(
                PROVIDER,
                f"Google Sheets rejected the request (HTTP {status}): {_message(resp)}",
                details=details,
            )
        if not resp.content:
            return {}
        try:
            body = resp.json()
        except ValueError:
            raise ProviderMalformedResponseError(
                PROVIDER, "Google Sheets returned non-JSON", details=details
            ) from None
        if not isinstance(body, dict):
            raise ProviderMalformedResponseError(
                PROVIDER, "unexpected Google Sheets response", details=details
            )
        return body

    async def spreadsheet(self) -> SpreadsheetInfo:
        body = await self._send(
            "GET", self._base, params={"fields": "properties.title,sheets.properties.title"}
        )
        props = body.get("properties")
        sheets = body.get("sheets")
        if not isinstance(props, dict) or not isinstance(sheets, list):
            raise ProviderMalformedResponseError(PROVIDER, "spreadsheet metadata missing")
        titles = [
            str(s["properties"]["title"])
            for s in sheets
            if isinstance(s, dict) and isinstance(s.get("properties"), dict)
        ]
        return SpreadsheetInfo(title=str(props.get("title", "")), worksheets=titles)

    async def add_worksheet(self, title: str) -> None:
        await self._send(
            "POST",
            f"{self._base}:batchUpdate",
            json={"requests": [{"addSheet": {"properties": {"title": title}}}]},
        )

    async def read_values(self, worksheet: str, columns: int) -> list[list[str]]:
        rng = quote(a1(worksheet, f"A1:{column_letter(columns)}"), safe="")
        body = await self._send("GET", f"{self._base}/values/{rng}")
        values = body.get("values", [])
        if not isinstance(values, list):
            raise ProviderMalformedResponseError(PROVIDER, "values is not a list")
        return [[str(c) for c in row] if isinstance(row, list) else [] for row in values]

    async def write_rows(
        self, worksheet: str, rows: Sequence[tuple[int, Sequence[str | None]]]
    ) -> None:
        """One RAW batch. A ``None`` cell is sent as JSON null, which Google Sheets skips -
        that cell keeps whatever the team typed there."""
        if not rows:
            return
        data = [{"range": a1(worksheet, f"A{n}"), "values": [list(cells)]} for n, cells in rows]
        body = await self._send(
            "POST",
            f"{self._base}/values:batchUpdate",
            json={"valueInputOption": "RAW", "data": data},
        )
        if body.get("totalUpdatedRows") not in (None, len(rows)):
            raise ProviderMalformedResponseError(
                PROVIDER,
                "Google Sheets updated a different number of rows than requested",
                details={"requested": len(rows), "updated": body.get("totalUpdatedRows")},
            )
