"""Phase 10 security suite: operator auth, write protection, CORS, body limit, headers,
injection / traversal at the API boundary, no secret in any response, pipeline control API."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.errors import ConfigurationError
from app.demo import CORE_DEMO_PAGES, DemoOutputs, build_demo_outputs, demo_pipeline
from app.domain.models import utcnow
from app.domain.pipeline import Trigger
from app.main import create_app
from app.repositories.pipeline import InMemoryPipelineStore
from app.services.dashboard import DashboardBackend
from app.services.pipeline import PipelineService

TOKEN = "operator-token-for-tests-0123456789abcdef"
OPENAI_KEY = "sk-test-openai-key-never-returned-000111"
WRITE = {"X-Dashboard-Request": "1"}
AUTH = {"Authorization": f"Bearer {TOKEN}"}
READS = ("/api/v1/dashboard/overview", "/api/v1/leads", "/api/v1/review-queue",
         "/api/v1/pipeline/runs")  # fmt: skip


@pytest.fixture
async def demo(make_settings) -> DemoOutputs:
    return await build_demo_outputs(make_settings(), dict(CORE_DEMO_PAGES))


def backend(settings: Settings, demo: DemoOutputs, pipeline: PipelineService | None = None):
    return DashboardBackend(
        demo.outputs, demo.views, demo.world.qualifications, "demo",
        pipeline=pipeline or demo_pipeline(settings, demo),
    )  # fmt: skip


def app_client(settings: Settings, demo: DemoOutputs, **kw: Any) -> TestClient:
    return TestClient(create_app(settings, dashboard_backend=backend(settings, demo)), **kw)


@pytest.fixture
def secured(make_settings, demo) -> Iterator[TestClient]:
    settings = make_settings(operator_api_token=TOKEN, openai_api_key=OPENAI_KEY)
    with app_client(settings, demo) as client:
        yield client


@pytest.fixture
def local(make_settings, demo) -> Iterator[TestClient]:
    with app_client(make_settings(), demo) as client:
        yield client


# ================================================================== authentication
@pytest.mark.parametrize("path", READS)
def test_api_requires_the_operator_token_when_configured(secured, path) -> None:
    assert secured.get(path).status_code == 401
    assert secured.get(path, headers={"Authorization": "Bearer wrong-token"}).status_code == 401
    assert secured.get(path, headers=AUTH).status_code == 200
    assert secured.get(path, headers={"X-Operator-Token": TOKEN}).status_code == 200


def test_health_and_page_stay_open_and_minimal(secured) -> None:
    health = secured.get("/health")
    assert health.status_code == 200 and set(health.json()) == {"status", "app", "version"}
    assert secured.get("/health/ready").status_code == 200
    assert secured.get("/dashboard").status_code == 200  # static page, no data


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/v1/pipeline/runs", {"requested_by": "x"}),
        ("/api/v1/pipeline/recover", {}),
        ("/api/v1/review/00000000-0000-0000-0000-000000000001",
         {"action": "reject", "reviewer": "x", "reason": "nope"}),
    ],
)  # fmt: skip
def test_write_endpoints_need_token_and_write_header(secured, path, body) -> None:
    assert secured.post(path, json=body, headers=WRITE).status_code == 401  # no token
    assert secured.post(path, json=body, headers=AUTH).status_code == 403  # no write header
    ok = secured.post(path, json=body, headers={**AUTH, **WRITE})
    assert ok.status_code in (200, 202, 404)  # authorised (404 = unknown company)


def test_without_a_token_only_loopback_clients_are_served(make_settings, demo) -> None:
    settings = make_settings()
    with app_client(settings, demo, client=("10.1.2.3", 50000)) as remote:
        resp = remote.get("/api/v1/leads")
        assert resp.status_code == 403 and "OPERATOR_API_TOKEN" in resp.text
        assert remote.get("/health").status_code == 200
    with app_client(settings, demo, client=("127.0.0.1", 50000)) as loopback:
        assert loopback.get("/api/v1/leads").status_code == 200


@pytest.mark.parametrize("env", ["staging", "production"])
def test_production_profile_refuses_to_start_without_a_token(make_settings, env) -> None:
    db = {"database_url": "postgresql+psycopg://u:p@127.0.0.1:5432/x"}  # required in production
    with pytest.raises(ConfigurationError, match="requires OPERATOR_API_TOKEN"):
        create_app(make_settings(app_env=env, **db))
    with pytest.raises(ConfigurationError, match="at least 24"):
        create_app(make_settings(app_env=env, operator_api_token="short", **db))
    create_app(make_settings(app_env=env, operator_api_token=TOKEN, **db))


def test_short_token_is_rejected_everywhere(make_settings) -> None:
    with pytest.raises(ConfigurationError, match="at least 24"):
        create_app(make_settings(operator_api_token="tooshort"))


# ================================================================== secrets never leave
def test_no_secret_in_any_response(secured) -> None:
    blobs = [secured.get(p, headers=AUTH).text for p in (*READS, "/health/ready", "/health")]
    blobs.append(secured.get("/api/v1/leads", headers={"Authorization": "Bearer nope"}).text)
    blobs.append(secured.get("/dashboard").text)
    joined = "\n".join(blobs)
    assert TOKEN not in joined and OPENAI_KEY not in joined
    providers = secured.get("/api/v1/dashboard/overview", headers=AUTH).json()["providers"]
    assert all(set(p) == {"provider", "mode", "configured", "operational", "paid", "detail",
                          "checked"} for p in providers)  # fmt: skip


# ================================================================== CORS
def test_cors_is_off_by_default(local) -> None:
    resp = local.get("/api/v1/leads", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in resp.headers
    preflight = local.options(
        "/api/v1/pipeline/runs",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in preflight.headers


def test_cors_allow_list_is_exact(make_settings, demo) -> None:
    settings = make_settings(cors_allowed_origins="https://ops.example.com")
    with app_client(settings, demo) as client:
        ok = client.get("/api/v1/leads", headers={"Origin": "https://ops.example.com"})
        assert ok.headers["access-control-allow-origin"] == "https://ops.example.com"
        other = client.get("/api/v1/leads", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in other.headers


@pytest.mark.parametrize("value", ["*", "https://*.example.com", "ops.example.com"])
def test_wildcard_or_malformed_cors_is_refused(make_settings, value) -> None:
    with pytest.raises(ConfigurationError, match="exact http"):
        create_app(make_settings(cors_allowed_origins=value))


# ================================================================== limits + headers
def test_oversized_body_is_rejected(make_settings, demo) -> None:
    settings = make_settings(max_request_body_bytes=2048)
    with app_client(settings, demo) as client:
        big = {"requested_by": "x" * 5000}
        resp = client.post("/api/v1/pipeline/runs", json=big, headers=WRITE)
        assert resp.status_code == 413 and resp.json()["error"]["code"] == "payload_too_large"
        small = client.post("/api/v1/pipeline/runs", json={"requested_by": "ok"}, headers=WRITE)
        assert small.status_code == 202


def test_security_headers(local) -> None:
    page = local.get("/dashboard")
    assert page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    api = local.get("/api/v1/leads")
    assert api.headers["cache-control"] == "no-store"
    assert api.headers["referrer-policy"] == "no-referrer"


# ================================================================== injection / traversal
@pytest.mark.parametrize(
    "url",
    [
        "/api/v1/leads?decision=qualified' OR '1'='1",
        "/api/v1/leads?limit=1;DROP TABLE companies",
        "/api/v1/leads?offset=-1",
        "/api/v1/leads/1 OR 1=1",
        "/api/v1/leads/../../.env",
        "/api/v1/pipeline/runs/not-a-uuid",
    ],
)
def test_malicious_parameters_are_rejected_by_validation(local, url) -> None:
    assert local.get(url).status_code in (404, 422)


TRAVERSAL = (
    "/dashboard/../.env",
    "/dashboard/%2e%2e/%2e%2e/.env",
    "/.env",
    "/secrets/google/service-account.json",
    "/static/../.env",
)


@pytest.mark.parametrize("path", TRAVERSAL)
def test_no_file_can_be_read_through_the_app(local, path) -> None:
    resp = local.get(path)
    assert resp.status_code in (404, 405) or "AI Lead Intelligence Engine" in resp.text
    assert "OPENAI_API_KEY" not in resp.text and "private_key" not in resp.text


def test_sql_text_in_free_text_fields_is_stored_as_text(local) -> None:
    company = local.get("/api/v1/review-queue").json()["items"][0]["company_id"]
    evil = "x'); DROP TABLE qualification_decisions; --"
    resp = local.post(f"/api/v1/review/{company}", headers=WRITE,
                      json={"action": "reject", "reviewer": evil, "reason": evil})  # fmt: skip
    assert resp.status_code == 200
    detail = local.get(f"/api/v1/leads/{company}").json()
    assert detail["reviews"][0]["reviewer"] == evil  # data, never executed


# ================================================================== pipeline control
def wait_done(client: TestClient, run_id: str) -> dict[str, Any]:
    for _ in range(100):
        run = client.get(f"/api/v1/pipeline/runs/{run_id}").json()
        if run["status"] != "running":
            return dict(run)
        time.sleep(0.05)
    raise AssertionError("pipeline run did not finish")


def test_trigger_dry_run_and_watch_it(local) -> None:
    resp = local.post("/api/v1/pipeline/runs", json={"requested_by": "Asha"}, headers=WRITE)
    assert resp.status_code == 202 and resp.json()["dry_run"] is True
    run = wait_done(local, resp.json()["run_id"])
    assert run["status"] == "succeeded" and run["trigger"] == "api"
    assert run["requested_by"] == "Asha" and run["dry_run"] is True
    assert [s["stage"] for s in run["stages"]][-1] == "export"
    overview = local.get("/api/v1/dashboard/overview").json()["pipeline_runs"]
    assert overview["available"] and overview["runs"][0]["run_id"] == run["run_id"]
    assert local.get("/api/v1/pipeline/runs").json()["items"][0]["run_id"] == run["run_id"]


def test_trigger_validation(local) -> None:
    def post(body: dict[str, Any]) -> Any:
        return local.post("/api/v1/pipeline/runs", json=body, headers=WRITE)

    assert post({}).status_code == 422  # requested_by is mandatory
    assert post({"requested_by": "a", "stages": ["outreach"]}).status_code == 422
    assert post({"requested_by": "a", "limit": 500}).status_code == 422
    chosen = post({"requested_by": "a", "stages": ["export"], "limit": 1})
    assert wait_done(local, chosen.json()["run_id"])["company_limit"] == 1


def test_writing_run_is_refused_in_mock_mode_and_duplicates_are_refused(
    make_settings, demo
) -> None:
    settings = make_settings()
    strict = PipelineService(InMemoryPipelineStore(), {}, settings)  # no mock writes allowed
    app = create_app(settings, dashboard_backend=backend(settings, demo, strict))
    with TestClient(app) as client:
        live = client.post("/api/v1/pipeline/runs", headers=WRITE,
                           json={"requested_by": "a", "dry_run": False})  # fmt: skip
        assert live.status_code == 409 and "PROVIDER_MODE=live" in live.text
        import asyncio

        held = strict.new_run(trigger=Trigger.CLI)
        asyncio.run(strict.start(held))
        dup = client.post("/api/v1/pipeline/runs", json={"requested_by": "a"}, headers=WRITE)
        assert dup.status_code == 409 and "already in progress" in dup.text
        # stale -> recover endpoint frees the slot
        held.heartbeat_at = utcnow() - timedelta(hours=5)
        strict._store.save_run(held)
        rec = client.post("/api/v1/pipeline/recover", json={}, headers=WRITE)
        assert rec.status_code == 200 and rec.json()["recovered"][0]["status"] == "abandoned"
        again = client.post("/api/v1/pipeline/runs", json={"requested_by": "a"}, headers=WRITE)
        assert again.status_code == 202


def test_only_expected_write_endpoints_exist(local) -> None:
    spec = local.get("/openapi.json").json()["paths"]
    writes = sorted(p for p, ops in spec.items() if {"post", "put", "patch", "delete"} & set(ops))
    assert writes == ["/api/v1/pipeline/recover", "/api/v1/pipeline/runs",
                      "/api/v1/review/{company_id}"]  # fmt: skip
    assert json.dumps(spec).count("delete") == 0
