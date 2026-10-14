"""Phase 6 CLI: dry run by default, gate honoured in the demo, unsafe/unready modes refused."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _no_dotenv(tmp_path, monkeypatch) -> None:
    """Scripts build ``Settings()`` from ``.env`` in the CWD - run them where none exists."""
    monkeypatch.chdir(tmp_path)


def load_cli() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "run_enrichment", ROOT / "scripts" / "run_enrichment.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dry_run_is_offline_and_shows_the_required_fields(capsys) -> None:
    assert load_cli().main([]) == 0
    out = capsys.readouterr().out
    assert "mode: mock providers | data: DEMO (synthetic, in-memory)" in out
    for field in (
        "Company:", "Qualification ID:", "Person:", "Title:", "Hunter result:", "Apollo result:",
        "Email:", "Verification:", "Confidence:", "Provider:", "Cost:", "FINAL STATUS:",
    ):  # fmt: skip
        assert field in out
    assert out.count("FINAL STATUS:     VERIFIED") == 2
    assert "FINAL STATUS:     BLOCKED" in out  # the REVIEW company never reaches a provider
    assert '"persisted": false' in out


def test_dry_run_json_output(capsys) -> None:
    assert load_cli().main(["--json", "--people-search", "hunter"]) == 0
    assert '"qualification_id"' in capsys.readouterr().out


@pytest.mark.parametrize(
    ("argv", "problem"),
    [
        (["--persist"], "--persist requires --db"),
        (["--live"], "--live requires --db"),
        (["--live"], "--live requires PROVIDER_MODE=live"),
        (["--live"], "--live requires ALLOW_PAID_PROVIDER_CALLS=true"),
        (["--limit", "0"], "--limit must be 1..50"),
        (["--db"], "database schema not ready"),
    ],
)
def test_unsafe_or_unready_modes_are_refused(capsys, argv, problem) -> None:
    assert load_cli().main(argv) == 2
    assert problem in capsys.readouterr().out
