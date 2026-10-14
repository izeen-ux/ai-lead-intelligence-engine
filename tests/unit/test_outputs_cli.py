"""Phase 8 CLIs: export dry run by default + refusals; dashboard launcher refusals."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENV = (
    "PROVIDER_MODE", "ALLOW_PAID_PROVIDER_CALLS", "ALLOW_SHEETS_WRITES",
    "GOOGLE_SHEETS_SPREADSHEET_ID", "GOOGLE_SERVICE_ACCOUNT_JSON", "DATABASE_URL",
)  # fmt: skip


@pytest.fixture(autouse=True)
def _no_dotenv(tmp_path, monkeypatch) -> None:
    """Scripts build ``Settings()`` from ``.env`` in the CWD - run them where none exists."""
    monkeypatch.chdir(tmp_path)
    for var in ENV:
        monkeypatch.delenv(var, raising=False)


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_is_an_offline_dry_run(capsys) -> None:
    assert load("export_leads_to_sheets").main([]) == 0
    out = capsys.readouterr().out
    assert "mode: DRY RUN | data: DEMO (synthetic, in-memory; never written to a Sheet)" in out
    assert "tab: Ai Leads" in out and "[Ai Leads] insert 7" in out  # 6 qualified + 1 review
    assert "Review_Queue" not in out and "FIXTURE_" not in out and "Export_Runs" not in out
    assert '"mode": "dry_run"' in out and '"persisted": false' in out and '"api_calls": 0' in out


def test_there_is_no_fixture_option_any_more() -> None:
    with pytest.raises(SystemExit):
        load("export_leads_to_sheets").main(["--fixture"])


@pytest.mark.parametrize(
    ("argv", "problem"),
    [
        (["--live"], "--live requires --db (real leads)"),
        (["--live"], "ALLOW_SHEETS_WRITES=true"),
        (["--live"], "GOOGLE_SHEETS_SPREADSHEET_ID is not set"),
        (["--live"], "service-account key file not found"),
        (["--check"], "GOOGLE_SHEETS_SPREADSHEET_ID is not set"),
        (["--live", "--dry-run"], "mutually exclusive"),
        (["--live", "--check"], "--check is read-only"),
        (["--persist"], "--persist requires --db"),
        (["--limit", "0"], "--limit must be 1..500"),
        (["--db"], "database schema not ready"),
    ],
)
def test_unsafe_or_unready_modes_are_refused(capsys, argv, problem) -> None:
    assert load("export_leads_to_sheets").main(argv) == 2
    assert problem in capsys.readouterr().out


def test_writes_need_the_process_switch_even_when_configured(capsys, tmp_path, monkeypatch) -> None:
    key = tmp_path / "sa.json"
    key.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GOOGLE_SHEETS_SPREADSHEET_ID", "1abc")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", str(key))
    assert load("export_leads_to_sheets").main(["--live"]) == 2
    out = capsys.readouterr().out
    assert "ALLOW_SHEETS_WRITES=true" in out and "1abc" not in out


@pytest.mark.parametrize(
    ("argv", "problem"),
    [(["--port", "80"], "--port must be 1024..65535"), ([], "database schema not ready")],
)
def test_dashboard_launcher_refusals(capsys, argv, problem) -> None:
    assert load("run_dashboard").main(argv) == 2
    assert problem in capsys.readouterr().out
