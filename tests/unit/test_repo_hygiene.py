"""Guards against configuration drift and committed secrets."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[2]
SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|DATABASE_URL)$")


def _env_example() -> dict[str, str]:
    pairs = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            pairs[key.strip()] = value.strip()
    return pairs


def test_env_example_documents_every_setting() -> None:
    documented = set(_env_example())
    expected = {name.upper() for name in Settings.model_fields}
    assert expected == documented, f"missing={expected - documented} extra={documented - expected}"


def test_env_example_contains_no_secret_values() -> None:
    for key, value in _env_example().items():
        if SECRET_NAME.search(key):
            assert value == "", f"{key} must be empty in .env.example"


def test_env_example_parses_into_valid_settings() -> None:
    s = Settings(_env_file=ROOT / ".env.example")
    assert s.provider_mode.value == "mock"
    assert s.allow_paid_provider_calls is False


def _git_ignored(path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "-q", path],  # noqa: S607
        cwd=ROOT,
        check=False,
    )
    assert result.returncode in (0, 1), "git check-ignore failed (is this a git repo?)"
    return result.returncode == 0


def test_dotenv_is_git_ignored() -> None:
    assert _git_ignored(".env")
    assert _git_ignored(".env.local")
    assert not _git_ignored(".env.example")  # the template must stay tracked


def test_no_obvious_secrets_in_source() -> None:
    patterns = [
        re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),  # Anthropic
        re.compile(r"apify_api_[A-Za-z0-9]{20,}"),  # Apify
        re.compile(r"fc-[a-f0-9]{32}"),  # Firecrawl
        re.compile(r"-----BEGIN (RSA |EC )?PRIVATE KEY-----"),
    ]
    for path in [*(ROOT / "app").rglob("*.py"), ROOT / ".env.example"]:
        text = path.read_text(encoding="utf-8")
        for p in patterns:
            assert not p.search(text), f"possible secret in {path}"
