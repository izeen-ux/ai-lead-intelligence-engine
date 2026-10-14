"""Google Sheets output (Phase 8). An OUTPUT sink, not a lead-data provider: it is not in the
provider registry and never feeds data back into PostgreSQL."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from app.providers.google_sheets.auth import (
    ServiceAccountTokenProvider,
    StaticTokenProvider,
    service_account_email,
)
from app.providers.google_sheets.client import GoogleSheetsWriter, SpreadsheetInfo
from app.providers.google_sheets.mock import InMemorySheetsWriter


class SheetsWriter(Protocol):
    """Capability used by the export service. ``mode`` is ``live`` or ``mock``."""

    mode: str
    api_calls: int

    @property
    def identity(self) -> str | None: ...

    async def spreadsheet(self) -> SpreadsheetInfo: ...

    async def add_worksheet(self, title: str) -> None: ...

    async def read_values(self, worksheet: str, columns: int) -> list[list[str]]: ...

    async def write_rows(
        self, worksheet: str, rows: Sequence[tuple[int, Sequence[str | None]]]
    ) -> None: ...


__all__ = [
    "GoogleSheetsWriter",
    "InMemorySheetsWriter",
    "ServiceAccountTokenProvider",
    "SheetsWriter",
    "SpreadsheetInfo",
    "StaticTokenProvider",
    "service_account_email",
]
