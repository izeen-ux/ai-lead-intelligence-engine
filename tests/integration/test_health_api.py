"""API-level tests through the full FastAPI stack (middleware, handlers, lifespan)."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from app import __version__
from app.core.config import Settings
from app.core.errors import ProviderCallBlockedError
from app.main import create_app


def test_liveness(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "app": "ai-lead-intelligence", "version": __version__}
    assert r.headers["X-Request-ID"]


def test_readiness_in_mock_mode(client: TestClient) -> None:
    r = client.get("/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["provider_mode"] == "mock"
    assert body["paid_provider_calls_allowed"] is False
    checks = {c["name"]: c["state"] for c in body["checks"]}
    assert checks == {
        "configuration": "ok",
        "provider_mode": "ok",
        "database": "not_configured",
        "database_schema": "not_configured",
        "enrichment": "not_configured",  # Phase 6: no sidecar URL in tests
    }
    assert len(body["providers"]) == 9  # + ai_router (Phase 5)


def test_readiness_degraded_in_live_mode_without_credentials(make_settings) -> None:
    with TestClient(create_app(make_settings(provider_mode="live"))) as c:
        body = c.get("/health/ready").json()
    assert body["status"] == "degraded"
    by_name = {p["name"]: p for p in body["providers"]}
    # paid providers without credentials are never operational
    assert all(not p["operational"] for p in body["providers"] if p["paid"])
    # Agent-Reach is keyless: the public news RSS channel is available (no network needed to tell)
    assert by_name["agent_reach"]["operational"] is True


def test_readiness_error_503_when_staging_has_no_database(make_settings) -> None:
    settings = make_settings(app_env="staging", operator_api_token="t" * 32)  # Phase 10: required
    with TestClient(create_app(settings)) as c:
        r = c.get("/health/ready")
    assert r.status_code == 503
    assert r.json()["status"] == "error"


def test_readiness_never_leaks_secrets(make_settings) -> None:
    s = make_settings(provider_mode="live", apollo_api_key="apollo-secret-value-xyz")
    with TestClient(create_app(s)) as c:
        text = c.get("/health/ready").text
    assert "apollo-secret-value-xyz" not in text


def test_incoming_request_id_is_propagated(client: TestClient) -> None:
    r = client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert r.headers["X-Request-ID"] == "abc-123"
    unsafe = client.get("/health", headers={"X-Request-ID": "bad id\nwith newline"})
    assert unsafe.headers["X-Request-ID"] != "bad id\nwith newline"


def test_404_uses_error_envelope(client: TestClient) -> None:
    r = client.get("/nope", headers={"X-Request-ID": "rid-404"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
    assert r.json()["error"]["request_id"] == "rid-404"


def _app_with_failing_routes(settings: Settings) -> FastAPI:
    app = create_app(settings)
    router = APIRouter()

    @router.get("/_test/provider-blocked")
    def blocked() -> None:
        raise ProviderCallBlockedError("hunter", "blocked", details={"operation": "enrich"})

    @router.get("/_test/crash")
    def crash() -> None:
        raise RuntimeError("database password is hunter2")

    @router.get("/_test/validate")
    def validate(limit: int) -> dict[str, int]:
        return {"limit": limit}

    app.include_router(router)
    return app


def test_app_error_maps_to_status_and_envelope(settings) -> None:
    with TestClient(_app_with_failing_routes(settings)) as c:
        r = c.get("/_test/provider-blocked")
    assert r.status_code == 503
    err = r.json()["error"]
    assert err["code"] == "provider_call_blocked"
    assert err["details"] == {"provider": "hunter", "operation": "enrich"}
    assert err["request_id"]


def test_unhandled_error_is_generic_500_without_internals(settings) -> None:
    with TestClient(_app_with_failing_routes(settings), raise_server_exceptions=False) as c:
        r = c.get("/_test/crash")
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "internal_error"
    assert "hunter2" not in r.text


def test_request_validation_error_envelope(settings) -> None:
    with TestClient(_app_with_failing_routes(settings)) as c:
        r = c.get("/_test/validate", params={"limit": "many"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "request_validation_error"
