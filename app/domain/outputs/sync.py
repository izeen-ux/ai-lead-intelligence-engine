"""Idempotent upsert planning for one worksheet (pure: no I/O).

Row 1 is the header. Column A holds the stable key (``lead_id`` / ``run_id``). For every
desired row the plan says: INSERT (key not in the sheet), UPDATE (key present, cells differ),
UNCHANGED (key present, identical) or SKIP (row-write limit reached). Keys found more than once
in the sheet are reported as duplicates; only the first occurrence is ever written, and no row
is ever appended for a key that already exists - so repeated runs never create duplicates.
Rows whose key is unknown to the application (manual notes) are left untouched.

The team may edit the worksheet: insert own columns (notes, call status, an empty spacer) or
move columns. As long as column A is still the key and every expected column NAME is present
exactly once, the plan writes each value under its own header and never touches the other
columns (``SheetPlan.positions``). Only a renamed / deleted expected column is refused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RowAction(StrEnum):
    INSERT = "insert"
    UPDATE = "update"
    UNCHANGED = "unchanged"
    WITHDRAW = "withdraw"  # an UPDATE that marks a no-longer-qualified lead
    SKIP = "skip"
    FAIL = "fail"


@dataclass(frozen=True)
class LegacyLayout:
    """A previous header of the same worksheet and how its column names map to today's."""

    header: tuple[str, ...]
    renames: dict[str, str] = field(default_factory=dict)


class HeaderMismatchError(ValueError):
    """Row 1 of the worksheet is not (a prefix of) the expected header - refuse to write."""


@dataclass(frozen=True)
class DesiredRow:
    key: str
    cells: list[str]
    withdraw: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PlannedRow:
    desired: DesiredRow
    action: RowAction
    row_number: int | None  # 1-based sheet row; None for SKIP


@dataclass
class SheetPlan:
    header_write: bool = False  # row 1 must be (re)written
    # Layout conversion: (row number, cells) of existing rows that are NOT rewritten by this
    # plan and therefore must be moved to the new column positions (same values, by name).
    relayout: list[tuple[int, list[str]]] = field(default_factory=list)
    # Width of the layout being replaced: every written row is padded to it so cells of
    # dropped columns are cleared instead of left behind (0 = no conversion).
    clear_width: int = 0
    # Team-edited layout: 0-based sheet column of each header column (None = our own layout).
    positions: list[int] | None = None
    rows: list[PlannedRow] = field(default_factory=list)
    duplicates: dict[str, list[int]] = field(default_factory=dict)

    @property
    def writes(self) -> list[PlannedRow]:
        return [
            r
            for r in self.rows
            if r.action in (RowAction.INSERT, RowAction.UPDATE, RowAction.WITHDRAW)
        ]

    def count(self, action: RowAction) -> int:
        return sum(1 for r in self.rows if r.action is action)


def pad(row: list[str], width: int) -> list[str]:
    """Sheets omits trailing empty cells on read; compare at full width."""
    cells = [str(c) for c in row[:width]]
    return cells + [""] * (width - len(cells))


def key_index(existing: list[list[str]]) -> tuple[dict[str, int], dict[str, list[int]], int]:
    """(key -> first row number, duplicates, last used row number) from the full sheet."""
    index: dict[str, int] = {}
    dupes: dict[str, list[int]] = {}
    last = len(existing)  # existing includes row 1 (header)
    for number, row in enumerate(existing[1:], start=2):
        key = (row[0] if row else "").strip()
        if not key:
            continue
        if key in index:
            dupes.setdefault(key, [index[key]]).append(number)
        else:
            index[key] = number
    return index, dupes, last


def _row1(existing: list[list[str]]) -> list[str]:
    current = [c.strip() for c in (existing[0] if existing else [])]
    while current and not current[-1]:
        current.pop()
    return current


def is_legacy(existing: list[list[str]], legacy: LegacyLayout | None) -> bool:
    return legacy is not None and _row1(existing) == list(legacy.header)


def relayout(row: list[str], legacy: LegacyLayout, header: tuple[str, ...]) -> list[str]:
    """One row of the legacy layout -> today's layout: each value moves to the column with
    the same (or renamed) name; columns that did not exist before stay empty."""
    old = dict(zip(legacy.header, pad(row, len(legacy.header)), strict=True))
    by_name = {legacy.renames.get(name, name): value for name, value in old.items()}
    return [by_name.get(col, "") for col in header]


def column_positions(existing: list[list[str]], header: tuple[str, ...]) -> list[int] | None:
    """Sheet column of every expected column when the team changed the layout (extra or moved
    columns) but kept all our column names; None when row 1 is not such a layout."""
    current = _row1(existing)
    if not current or current[0] != header[0] or current == list(header):
        return None
    if any(current.count(name) != 1 for name in header):
        return None
    return [current.index(name) for name in header]


def project(row: list[str], positions: list[int]) -> list[str]:
    """The cells of one sheet row that belong to our columns, in header order."""
    return [str(row[i]) if i < len(row) else "" for i in positions]


def spread(cells: list[str], positions: list[int]) -> list[str | None]:
    """Our cells placed at their sheet columns; None = a team column that is not written."""
    out: list[str | None] = [None] * (max(positions) + 1)
    for cell, index in zip(cells, positions, strict=True):
        out[index] = cell
    return out


def check_header(
    existing: list[list[str]], header: tuple[str, ...], legacy: LegacyLayout | None = None
) -> bool:
    """Return True when row 1 must be written. Raises if row 1 belongs to something else."""
    current = _row1(existing)
    if current == list(header):
        return False
    if not current or current == list(header[: len(current)]):
        return True  # empty tab, or new columns appended at the end of our own header
    if is_legacy(existing, legacy):
        return True  # our own previous layout: converted in place by plan_upsert
    if column_positions(existing, header) is not None:
        return False  # team-edited layout with every column name intact: write by name
    missing = [name for name in header if current.count(name) != 1]
    raise HeaderMismatchError(
        f"row 1 of the worksheet was changed: column(s) {missing[:5]!r} are missing, renamed "
        f"or duplicated (row 1 has {len(current)} columns starting {current[:3]!r}). Restore "
        "the header names in row 1; extra columns of your own are fine"
    )


def plan_upsert(
    existing: list[list[str]],
    header: tuple[str, ...],
    desired: list[DesiredRow],
    *,
    max_writes: int,
    legacy: LegacyLayout | None = None,
) -> SheetPlan:
    plan = SheetPlan(header_write=check_header(existing, header, legacy))
    converting = is_legacy(existing, legacy)
    positions = None if converting else column_positions(existing, header)
    plan.positions = positions
    index, dupes, last = key_index(existing)
    plan.duplicates = dupes
    width = len(header)

    def current(number: int) -> list[str]:
        row = existing[number - 1]
        return project(row, positions) if positions is not None else pad(row, width)

    next_row = max(last, 1) + 1
    writes = 0
    seen: set[str] = set()
    for row in desired:
        if row.key in seen:  # the application never emits a key twice; guard anyway
            continue
        seen.add(row.key)
        cells = pad(row.cells, width)
        number = index.get(row.key)
        if number is not None and not converting and current(number) == cells:
            plan.rows.append(PlannedRow(row, RowAction.UNCHANGED, number))
            continue
        if writes >= max_writes:
            plan.rows.append(PlannedRow(row, RowAction.SKIP, None))
            continue
        writes += 1
        if number is None:
            plan.rows.append(PlannedRow(row, RowAction.INSERT, next_row))
            next_row += 1
        else:
            action = RowAction.WITHDRAW if row.withdraw else RowAction.UPDATE
            plan.rows.append(PlannedRow(row, action, number))
    if converting and legacy is not None:
        plan.clear_width = len(legacy.header)
        rewritten = {p.row_number for p in plan.writes}
        plan.relayout = [
            (number, relayout(row, legacy, header))
            for number, row in enumerate(existing[1:], start=2)
            if number not in rewritten and any(str(c).strip() for c in row)
        ]
    return plan
