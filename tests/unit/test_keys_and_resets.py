"""Key management command (scripts/keys.py) and credit reset dates (owner request 2026-10-14):
paste a key without editing files; see when every provider gives new credits."""

from __future__ import annotations

import importlib.util
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.services.credits import (
    CreditLine,
    KeyReading,
    apify_reading,
    firecrawl_reading,
    first_of_next_month,
    hunter_reading,
    key_rows,
    summed,
)

ROOT = Path(__file__).resolve().parents[2]


def load_keys() -> ModuleType:
    spec = importlib.util.spec_from_file_location("keys_cli", ROOT / "scripts" / "keys.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ================================================================== reset dates
def test_each_provider_reports_when_its_credits_come_back() -> None:
    apify = apify_reading({"data": {
        "monthlyUsageCycle": {"startAt": "2026-09-25T00:00:00.000Z",
                              "endAt": "2026-10-24T23:59:59.999Z"},
        "current": {"monthlyUsageUsd": 4.07}, "limits": {"maxMonthlyUsageUsd": 5},
    }})  # fmt: skip
    assert apify == KeyReading(Decimal("4.07"), Decimal(5), "2026-10-25")  # day after cycle end
    fire = firecrawl_reading({"data": {
        "remainingCredits": 47, "planCredits": 1000, "billingPeriodEnd": "2026-10-29T05:22:09Z",
    }})  # fmt: skip
    assert fire == KeyReading(Decimal(953), Decimal(1000), "2026-10-29")
    hunter = hunter_reading(
        {
            "data": {
                "reset_date": "2026-10-25",
                "requests": {"searches": {"used": 24, "available": 50}},
            }
        }
    )
    assert hunter == KeyReading(Decimal(24), Decimal(50), "2026-10-25")
    assert apify_reading({}).resets_on is None  # nothing reported -> no invented date
    assert first_of_next_month(datetime(2026, 12, 14)) == "2027-01-01"


def test_keys_are_added_together_and_the_earliest_reset_is_shown() -> None:
    results: list[Any] = [
        KeyReading(Decimal(5), Decimal(5), "2026-10-25"),  # used up
        KeyReading(Decimal(0), Decimal(5), "2026-11-13"),
        RuntimeError("401"),
    ]
    rows = tuple(key_rows(results))
    assert [r["status"] for r in rows] == ["used up", "ok", "not readable"]
    line = summed("apify", "USD", [r for r in results if isinstance(r, KeyReading)], rows)
    assert isinstance(line, CreditLine)
    assert (line.used, line.remaining, line.limit, line.resets_on) == ("5", "5", "10", "2026-10-25")
    assert line.note == "3 keys added together (1 not accepted by the provider)"
    assert all("key" not in str(v).lower() or k == "position" for r in rows for k, v in r.items())


# ================================================================== scripts/keys.py
@pytest.mark.parametrize(
    "pasted",
    [
        "apify_api_ABCDEF123456",
        '  APIFY_TOKEN="apify_api_ABCDEF123456"  \n',
        "Bearer apify_api_ABCDEF123456",
        "'apify_api_ABCDEF123456'",
        "﻿apify_api_ABCDEF123456\r\n",
    ],
)
def test_pasted_keys_are_cleaned(pasted: str) -> None:
    assert load_keys().clean(pasted) == "apify_api_ABCDEF123456"


@pytest.fixture
def env(tmp_path, monkeypatch) -> tuple[ModuleType, Path]:
    keys = load_keys()
    path = tmp_path / ".env"
    path.write_text("# keep me\nAPIFY_TOKEN=apify_api_FIRST00000\nOTHER=1\n", encoding="utf-8")
    monkeypatch.setattr(keys, "ENV", path)
    monkeypatch.setenv("KEYS_NO_RESTART", "1")
    monkeypatch.setenv("APIFY_TOKEN", "")  # a shell variable must not hide the saved keys
    return keys, path


def answer(keys: ModuleType, monkeypatch, pasted: str, state: str = "ok") -> None:
    monkeypatch.setattr(keys.getpass, "getpass", lambda prompt="": pasted)

    async def fake_check(provider: str, key: str) -> Any:
        return state, KeyReading(Decimal(0), Decimal(5), "2026-11-13"), ""

    monkeypatch.setattr(keys, "check", fake_check)


def test_add_saves_a_checked_key_as_the_next_backup(env, monkeypatch, capsys) -> None:
    keys, path = env
    answer(keys, monkeypatch, '"apify_api_SECOND0000"')
    assert keys.main(["add", "apify"]) == 0
    text = path.read_text(encoding="utf-8")
    assert "APIFY_TOKEN=apify_api_FIRST00000" in text and "OTHER=1" in text and "# keep me" in text
    assert "APIFY_TOKEN_BACKUPS=apify_api_SECOND0000" in text
    out = capsys.readouterr().out
    assert "Saved as apify key 2 (backup 1)" in out and "2026-11-13" in out
    assert "SECOND0000" not in out  # the key is never printed
    answer(keys, monkeypatch, "apify_api_SECOND0000")
    assert keys.main(["add", "apify"]) == 0 and "already saved" in capsys.readouterr().out


def test_a_refused_or_broken_key_is_not_saved(env, monkeypatch, capsys) -> None:
    keys, path = env
    before = path.read_text(encoding="utf-8")
    answer(keys, monkeypatch, "apify_api_BADBAD0000", state="refused")
    assert keys.main(["add", "apify"]) == 1
    answer(keys, monkeypatch, "too short")
    assert keys.main(["add", "apify"]) == 2
    assert path.read_text(encoding="utf-8") == before


def test_claude_key_replaces_and_remove_drops_one_key(env, monkeypatch, capsys) -> None:
    keys, path = env
    answer(keys, monkeypatch, "sk-ant-api03-NEWKEY000000")
    assert keys.main(["add", "anthropic"]) == 0
    assert "ANTHROPIC_API_KEY=sk-ant-api03-NEWKEY000000" in path.read_text(encoding="utf-8")
    answer(keys, monkeypatch, "apify_api_SECOND0000")
    keys.main(["add", "apify"])
    assert keys.main(["remove", "apify", "1"]) == 0
    text = path.read_text(encoding="utf-8")
    assert "APIFY_TOKEN=apify_api_SECOND0000" in text and "FIRST" not in text
    assert keys.main(["remove", "apify", "5"]) == 2
