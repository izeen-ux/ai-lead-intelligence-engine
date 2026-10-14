"""Phase 7 CLIs: run_lead_analysis dry run by default + refusals; check_ai_router discovery via
MockTransport (no network) and refusal of authenticated steps without live switches/key."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import httpx2
import pytest

from app.providers.ai_router import AiRouterAdapter
from tests.unit.test_ai_router import KEY, Router

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _no_dotenv(tmp_path, monkeypatch) -> None:
    """Scripts build ``Settings()`` from ``.env`` in the CWD - run them where none exists."""
    monkeypatch.chdir(tmp_path)
    for var in (
        "PROVIDER_MODE",
        "ALLOW_PAID_PROVIDER_CALLS",
        "AI_ROUTER_API_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ================================================================== run_lead_analysis
def test_dry_run_is_offline_and_shows_the_required_fields(capsys) -> None:
    assert load("run_lead_analysis").main([]) == 0
    out = capsys.readouterr().out
    assert "mode: mock AI | data: DEMO (synthetic, in-memory)" in out
    for field in (
        "Company:", "Contact:", "Status:", "Primary trigger:", "Observed facts:",
        "Pain hypothesis:", "Opportunity:", "Recommended offer:", "Offer reason:",
        "Outreach hook:", "Confidence:", "Evidence count:", "Model/provider:",
    ):  # fmt: skip
        assert field in out
    assert out.count("Status:             SUCCEEDED") == 5  # five synthetic ready leads
    assert "Status:             BLOCKED" in out  # the REVIEW company never reaches the model
    assert '"persisted": false' in out


def test_dry_run_json_output(capsys) -> None:
    assert load("run_lead_analysis").main(["--json", "--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert '"recommended_offer_id"' in out and '"prompt_version"' in out


@pytest.mark.parametrize(
    ("argv", "problem"),
    [
        (["--persist"], "--persist requires --db"),
        (["--live"], "--live requires --db"),
        (["--live"], "--live requires PROVIDER_MODE=live"),
        (["--live"], "--live requires ALLOW_PAID_PROVIDER_CALLS=true"),
        (["--live"], "OPENAI_API_KEY is not set"),
        (["--limit", "0"], "--limit must be 1..50"),
        (["--db"], "database schema not ready"),
    ],
)
def test_unsafe_or_unready_modes_are_refused(capsys, argv, problem) -> None:
    assert load("run_lead_analysis").main(argv) == 2
    assert problem in capsys.readouterr().out


# ================================================================== check_ai_router
@pytest.fixture
def fake_router(monkeypatch) -> tuple[ModuleType, Router]:
    module = load("check_ai_router")
    router = Router()
    monkeypatch.setattr(
        module,
        "AiRouterAdapter",
        lambda s: AiRouterAdapter(s, transport=httpx2.MockTransport(router)),
    )
    return module, router


def test_discovery_without_key(capsys, fake_router) -> None:
    module, router = fake_router
    assert module.main([]) == 0
    out = capsys.readouterr().out
    assert "1. GET /health" in out and "4 enabled free" in out
    assert "primary google/gemma-4-31b-it:free" in out
    assert "key configured: NO" in out
    assert router.chats() == []


def test_authenticated_steps_refused_without_live_switches(capsys, fake_router) -> None:
    module, router = fake_router
    assert module.main(["--chat", "--analysis-sample"]) == 2
    out = capsys.readouterr().out
    assert "REFUSING authenticated steps" in out and "AI_ROUTER_API_KEY" in out
    assert router.chats() == []


def test_chat_and_sample_with_live_switches(capsys, fake_router, monkeypatch) -> None:
    module, router = fake_router
    monkeypatch.setenv("PROVIDER_MODE", "live")
    monkeypatch.setenv("ALLOW_PAID_PROVIDER_CALLS", "true")
    monkeypatch.setenv("AI_ROUTER_API_KEY", KEY)
    # The fake router answers "OK" to every chat - the structured sample must be REJECTED.
    assert module.main(["--chat", "--analysis-sample"]) == 3
    out = capsys.readouterr().out
    assert "5. POST /v1/chat/completions -> 'OK'" in out
    assert "OUTPUT REJECTED (malformed_json)" in out
    assert KEY not in out
    assert len(router.chats()) == 2
