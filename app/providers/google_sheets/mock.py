"""In-memory ``SheetsWriter`` (demo + tests). Behaves like the Sheets API for what we use:
trailing empty cells are dropped on read, rows are addressed 1-based, writes overwrite."""

from __future__ import annotations

from collections.abc import Sequence

from app.core.errors import ProviderError
from app.providers.google_sheets.client import SpreadsheetInfo


class InMemorySheetsWriter:
    mode = "mock"

    def __init__(self, title: str = "Demo workbook", identity: str | None = None) -> None:
        self.title = title
        self.tabs: dict[str, list[list[str]]] = {}
        self.api_calls = 0
        self.fail_next: list[ProviderError] = []  # raised (in order) by the next calls
        self._identity = identity

    @property
    def identity(self) -> str | None:
        return self._identity

    def _call(self) -> None:
        self.api_calls += 1
        if self.fail_next:
            raise self.fail_next.pop(0)

    async def spreadsheet(self) -> SpreadsheetInfo:
        self._call()
        return SpreadsheetInfo(self.title, list(self.tabs))

    async def add_worksheet(self, title: str) -> None:
        self._call()
        self.tabs.setdefault(title, [])

    async def read_values(self, worksheet: str, columns: int) -> list[list[str]]:
        self._call()
        rows = [list(r[:columns]) for r in self.tabs.get(worksheet, [])]
        for row in rows:
            while row and row[-1] == "":
                row.pop()
        while rows and not rows[-1]:
            rows.pop()
        return rows

    async def write_rows(
        self, worksheet: str, rows: Sequence[tuple[int, Sequence[str | None]]]
    ) -> None:
        self._call()
        tab = self.tabs.setdefault(worksheet, [])
        for number, cells in rows:
            while len(tab) < number:
                tab.append([])
            old = tab[number - 1]  # None = leave that cell alone (like the real API)
            tab[number - 1] = [
                (old[i] if i < len(old) else "") if cell is None else cell
                for i, cell in enumerate(cells)
            ] + (old[len(cells) :] if any(c is None for c in cells) else [])

    def keys(self, worksheet: str) -> list[str]:
        return [r[0] for r in self.tabs.get(worksheet, [])[1:] if r]
