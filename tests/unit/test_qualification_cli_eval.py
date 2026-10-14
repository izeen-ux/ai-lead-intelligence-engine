"""Phase 5 CLI (dry run default, guarded modes) and the labeled evaluation harness."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from app.domain.enums import QualificationDecision
from app.evaluation.qualification import DEFAULT_DATASET, evaluate, load_dataset, write_report
from app.providers.laya import MockLayaProvider

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _no_dotenv(tmp_path, monkeypatch) -> None:
    """Scripts build ``Settings()`` from ``.env`` in the CWD - run them where none exists."""
    monkeypatch.chdir(tmp_path)


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ================================================================== CLI
def test_cli_dry_run_is_offline_and_shows_all_three_outcomes(capsys) -> None:
    cli = load_script("run_qualification")
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "DEMO (synthetic labeled cases, in-memory)" in out
    for decision in ("QUALIFIED", "REVIEW", "NOT_QUALIFIED"):
        assert f"FINAL DECISION:   {decision}" in out
    assert '"persisted": false' in out


def test_cli_dry_run_with_llm_review_and_json(capsys) -> None:
    cli = load_script("run_qualification")
    assert cli.main(["--llm-review", "--json"]) == 0
    out = capsys.readouterr().out
    assert "llm review: enabled" in out
    assert '"llm_review_status": "ok"' in out  # the REVIEW demo case was routed to the reviewer


@pytest.mark.parametrize(
    ("argv", "problem"),
    [
        (["--persist"], "--persist requires --db"),
        (["--live"], "--live requires PROVIDER_MODE=live"),
        (["--limit", "0"], "--limit must be 1..100"),
        (["--db"], "database schema not ready"),
    ],
)
def test_cli_refuses_unsafe_or_unready_modes(capsys, argv, problem) -> None:
    cli = load_script("run_qualification")
    assert cli.main(argv) == 2
    assert problem in capsys.readouterr().out


# ================================================================== evaluation harness
def test_dataset_is_labeled_synthetic_and_covers_required_categories() -> None:
    ds = load_dataset(DEFAULT_DATASET)
    assert "SYNTHETIC" in ds.provenance
    required = {
        "clear_funding", "funding_above_cap", "funding_outside_window", "missing_funding_amount",
        "strong_ai_intent", "strong_expansion", "weak_generic_news", "irrelevant_mention",
        "namesake_noisy", "duplicate_coverage", "conflicting_evidence", "insufficient_evidence",
        "ambiguous_review",
    }  # fmt: skip
    assert required <= {c.category for c in ds.cases}
    assert len({c.id for c in ds.cases}) == len(ds.cases)
    assert {c.label for c in ds.cases} == set(QualificationDecision)


async def test_mock_evaluation_regression_guard(make_settings, tmp_path) -> None:
    """With the deterministic mock, the pipeline must reproduce every label: this guards the
    RULES against regressions. It says nothing about Laya's (or production) accuracy."""
    report = await evaluate(load_dataset(), MockLayaProvider(), make_settings())
    summary = report.as_dict()
    assert summary["false_qualified"] == []
    assert summary["hard_path_accuracy"] == 1.0
    assert summary["event_count_checks"] == "2/2"
    assert summary["decision_accuracy"] == 1.0, summary["mismatches"]
    out = tmp_path / "report.json"
    write_report(report, out)
    assert json.loads(out.read_text(encoding="utf-8"))["cases"] == len(report.results)


def test_evaluate_script_offline(capsys, tmp_path) -> None:
    script = load_script("evaluate_qualification")
    out = tmp_path / "r.json"
    assert script.main(["--out", str(out)]) == 0
    assert '"laya_mode": "mock"' in capsys.readouterr().out
    assert out.exists()
