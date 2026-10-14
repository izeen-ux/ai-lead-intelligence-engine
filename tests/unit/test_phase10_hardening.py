"""Phase 10: private-service rule, database host override, backup/restore helpers, deployment
files (non-root, lockfile, no public ports, no secrets), OpenAI check CLI, log redaction."""

from __future__ import annotations

import importlib.util
import io
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

from app.core.logging import (
    JsonFormatter,
    RedactionFilter,
    RequestIdFilter,
    pipeline_run_var,
    pipeline_stage_var,
)
from app.domain.website import crawlable_domain
from app.providers.enrichment_kit.client import endpoint_problem
from app.providers.laya import LayaAdapter
from app.utils.pg_backup import apply_retention, backup_name, database_identity

ROOT = Path(__file__).resolve().parents[2]


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ================================================================== private services
def test_sidecars_must_be_loopback_or_explicitly_allow_listed(make_settings) -> None:
    public = make_settings(enrichment_kit_base_url="http://sidecar.example.com:8787",
                           laya_endpoint_url="http://10.0.0.9:8765")  # fmt: skip
    assert "loopback" in (endpoint_problem(public) or "")
    assert "loopback" in (LayaAdapter(public)._endpoint_problem() or "")
    compose = make_settings(enrichment_kit_base_url="http://enrichment-kit:8787",
                            laya_endpoint_url="http://laya:8765",
                            internal_service_hosts="laya, enrichment-kit")  # fmt: skip
    assert endpoint_problem(compose) is None and LayaAdapter(compose)._endpoint_problem() is None
    assert compose.is_private_service_host("LAYA") and compose.is_private_service_host("::1")
    assert not compose.is_private_service_host("api.hunter.io")
    assert not make_settings().is_private_service_host("laya")  # nothing allowed by default


def test_database_host_override_changes_only_the_host(make_settings) -> None:
    url = "postgresql+psycopg://lead:s3cr3t-pw@localhost:5432/lead_intel"
    plain = make_settings(database_url=url)
    assert plain.effective_database_url == url
    inside = make_settings(database_url=url, database_host_override="postgres")
    assert inside.effective_database_url == url.replace("@localhost:", "@postgres:")
    assert make_settings().effective_database_url is None
    assert "s3cr3t-pw" not in repr(inside)  # the password stays a secret


# ================================================================== backup / restore helpers
def test_backup_names_are_timestamped_and_sortable() -> None:
    name = backup_name("lead_intel", datetime(2026, 10, 5, 9, 30, 15, tzinfo=UTC))
    assert name == "lead_intel_20261005T093015Z.dump"


def test_retention_keeps_the_newest(tmp_path) -> None:
    for day in range(1, 8):
        (tmp_path / f"lead_intel_2026100{day}T000000Z.dump").write_bytes(b"x")
    (tmp_path / "other_db_20260101T000000Z.dump").write_bytes(b"x")  # another database: untouched
    removed = apply_retention(tmp_path, "lead_intel", keep=3)
    assert len(removed) == 4
    left = sorted(p.name for p in tmp_path.glob("lead_intel_*.dump"))
    assert left == [f"lead_intel_2026100{d}T000000Z.dump" for d in (5, 6, 7)]
    assert (tmp_path / "other_db_20260101T000000Z.dump").exists()


def test_database_identity_never_returns_the_password(make_settings) -> None:
    s = make_settings(database_url="postgresql+psycopg://lead:s3cr3t-pw@localhost:5432/lead_intel")
    assert database_identity(s) == ("lead_intel", "lead")
    with pytest.raises(SystemExit, match="DATABASE_URL"):
        database_identity(make_settings())


@pytest.mark.parametrize(
    ("target", "problem"),
    [
        ("lead_intel", "NOT be the live database"),
        ("production_copy", "must start with"),
        ("lead_intel_restore; drop database x", "lowercase letters"),
        ("Lead_Intel_Restore", "lowercase letters"),
    ],
)
def test_restore_refuses_unsafe_targets(target, problem) -> None:
    restore = load("restore_db")
    assert problem in (restore.target_problem(target, "lead_intel") or "")
    assert restore.target_problem("lead_intel_restore_check", "lead_intel") is None


def test_restore_cli_refuses_before_touching_docker(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/lead_intel")
    restore = load("restore_db")
    assert restore.main(["--file", "missing.dump"]) == 2
    assert restore.main(["--file", "missing.dump", "--target-db", "lead_intel"]) == 2
    out = capsys.readouterr().out
    assert "backup file not found" in out and "NOT be the live database" in out


# ================================================================== deployment files
def test_app_image_is_reproducible_non_root_and_secret_free() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "uv sync --frozen --no-dev" in dockerfile  # lockfile only, no dev dependencies
    assert re.search(r"^USER app$", dockerfile, re.M) and "useradd --system" in dockerfile
    assert "HEALTHCHECK" in dockerfile and "STOPSIGNAL SIGTERM" in dockerfile
    assert not re.search(r"(API_KEY|TOKEN|PASSWORD)\s*=\s*\S", dockerfile)
    assert "COPY . " not in dockerfile  # only named paths are copied
    assert not re.search(r"^(COPY|ADD) .*(\.env|secrets)", dockerfile, re.M)
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert {".env", ".env.*", "secrets/", "backups/"} <= set(ignore)
    for name in ("laya.Dockerfile", "enrichment-kit.Dockerfile"):
        text = (ROOT / "docker" / name).read_text(encoding="utf-8")
        assert re.search(r"^USER (laya|node)$", text, re.M), name


def test_compose_publishes_loopback_only_and_keeps_sidecars_private() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    published = re.findall(r'^\s*- "([^"]+:\d+:\d+)"', compose, re.M)
    assert published and all(p.startswith(("127.0.0.1:", "[::1]:")) for p in published)
    assert not any(p.startswith("0.0.0.0") for p in published)  # noqa: S104
    for private in ("laya", "enrichment-kit"):
        block = compose.split(f"\n  {private}:\n", 1)[1].split("\n\n", 1)[0]
        assert not re.search(r"^\s+ports:", block, re.M), f"{private} must not publish a port"
    assert "external: true" in compose and "lead-intel-pgdata" in compose  # data volume reused
    assert '"alembic", "upgrade", "head"' in compose  # forward-only migration step
    for destructive in ('"downgrade"', "dropdb", "drop database", "down -v", "--volumes"):
        assert destructive not in compose.lower()
    assert "${PROVIDER_MODE:-mock}" in compose and "${ALLOW_PAID_PROVIDER_CALLS:-false}" in compose
    assert "${ALLOW_SHEETS_WRITES:-false}" in compose  # safe baseline unless opted in per run
    assert "cap_drop: [ALL]" in compose and "no-new-privileges:true" in compose
    assert not re.search(r"(sk-|tair_)[A-Za-z0-9]{8,}", compose)


def test_backups_and_env_are_git_ignored() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "backups/" in ignored and "secrets/" in ignored and ".env" in ignored


# ================================================================== OpenAI check CLI
def test_check_openai_refuses_without_a_key(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    for var in ("OPENAI_API_KEY", "PROVIDER_MODE", "ALLOW_PAID_PROVIDER_CALLS"):
        monkeypatch.delenv(var, raising=False)
    assert load("check_openai").main([]) == 2
    out = capsys.readouterr().out
    assert "key configured: NO" in out and "OPENAI_API_KEY is not set" in out
    assert "$0.25/run" in out and "$5.00/month" in out


# ================================================================== logging
def emit(secret_values: list[str], **extra: object) -> dict[str, object]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestIdFilter())
    handler.addFilter(RedactionFilter(secret_values))
    logger = logging.getLogger("phase10.test")
    logger.handlers, logger.propagate = [handler], False
    logger.setLevel(logging.INFO)
    try:
        raise RuntimeError("call to https://api.example/v1?key=sk-live-SECRET-123456 failed")
    except RuntimeError:
        logger.exception("stage crashed", extra=extra)
    return dict(json.loads(stream.getvalue()))


def test_log_lines_carry_run_context_and_redact_everywhere() -> None:
    run = pipeline_run_var.set("run-123")
    stage = pipeline_stage_var.set("analysis")
    try:
        line = emit(
            ["sk-live-SECRET-123456"],
            provider="openai",
            details={"authorization": "Bearer abc", "note": "used sk-live-SECRET-123456", "n": 3},
            items=["ok", "sk-live-SECRET-123456"],
        )
    finally:
        pipeline_run_var.reset(run)
        pipeline_stage_var.reset(stage)
    assert (line["pipeline_run_id"], line["stage"], line["provider"]) == ("run-123", "analysis",
                                                                           "openai")  # fmt: skip
    assert line["timestamp"] and line["level"] == "ERROR"
    blob = json.dumps(line)
    assert "sk-live-SECRET-123456" not in blob  # message, nested extras AND traceback
    assert line["details"] == {"authorization": "***REDACTED***",
                               "note": "used ***REDACTED***", "n": 3}  # fmt: skip
    assert "RuntimeError" in str(line["exception"])


def test_no_run_context_outside_a_pipeline() -> None:
    line = emit([])
    assert "pipeline_run_id" not in line and "stage" not in line


# ================================================================== SSRF
@pytest.mark.parametrize(
    "target",
    [
        "127.0.0.1", "10.0.0.5", "192.168.1.10", "169.254.169.254", "[::1]", "0.0.0.0",  # noqa: S104
        "2130706433", "postgres", "laya", "localhost", "intranet.local", "db.internal",
        "service.lan", "example.test",
    ],
)  # fmt: skip
def test_crawl_targets_can_never_be_internal(target: str) -> None:
    with pytest.raises(ValueError, match=r"not allowed|invalid|domain"):
        crawlable_domain(target)


@pytest.mark.parametrize("target", ["drivn.co.in", "www.example.com", "shop.acme.io"])
def test_public_company_domains_are_crawlable(target: str) -> None:
    assert crawlable_domain(target)
