"""Readiness/startup checks. Config-only for providers: this module never calls a provider."""

from __future__ import annotations

import httpx2

from app.core.config import Environment, ProviderMode, Settings
from app.db.session import check_database, check_schema
from app.domain.enums import ProviderName
from app.providers.enrichment_kit.client import endpoint_problem
from app.providers.registry import ProviderRegistry
from app.schemas.health import ComponentCheck, OverallStatus, ReadinessResponse


def _provider_mode_check(settings: Settings) -> ComponentCheck:
    if settings.provider_mode is ProviderMode.MOCK:
        return ComponentCheck(name="provider_mode", state="ok", detail="mock (offline)")
    missing = [p.value for p in settings.missing_provider_credentials()]
    if missing:
        return ComponentCheck(
            name="provider_mode",
            state="warning",
            detail="live mode; missing credentials for: " + ", ".join(missing),
        )
    return ComponentCheck(name="provider_mode", state="ok", detail="live")


def _database_check(settings: Settings) -> ComponentCheck:
    state, detail = check_database(settings)
    if state == "not_configured" and settings.app_env in (
        Environment.STAGING,
        Environment.PRODUCTION,
    ):
        return ComponentCheck(name="database", state="error", detail=detail)
    return ComponentCheck(
        name="database",
        state="ok" if state == "ok" else "not_configured" if state == "not_configured" else "error",
        detail=detail,
    )


def _schema_check(settings: Settings, database: ComponentCheck) -> ComponentCheck:
    if database.state != "ok":
        return ComponentCheck(name="database_schema", state="not_configured", detail="no database")
    state, detail = check_schema(settings)
    if state == "ok":
        return ComponentCheck(name="database_schema", state="ok", detail=detail)
    if state == "outdated":
        return ComponentCheck(name="database_schema", state="warning", detail=detail)
    return ComponentCheck(name="database_schema", state="error", detail=detail)


def _enrichment_check(settings: Settings) -> ComponentCheck:
    """Phase 6: sidecar URL + Hunter/Apollo key presence, and - only when a loopback sidecar
    URL is configured - its local ``GET /health`` (configuration + counters; never a Hunter or
    Apollo call, never credits). Keys are reported as present/absent only."""
    keys = {
        "hunter": settings.provider_has_credentials(ProviderName.HUNTER),
        "apollo": settings.provider_has_credentials(ProviderName.APOLLO),
    }
    keys_txt = ", ".join(f"{k}_key={'set' if v else 'missing'}" for k, v in keys.items())
    problem = endpoint_problem(settings)
    if problem is not None:
        return ComponentCheck(
            name="enrichment", state="not_configured", detail=f"{problem}; {keys_txt}"
        )
    try:
        with httpx2.Client(timeout=1.5) as client:
            body = client.get(str(settings.enrichment_kit_base_url).rstrip("/") + "/health").json()
    except (httpx2.HTTPError, ValueError):
        return ComponentCheck(
            name="enrichment",
            state="warning",
            detail=f"sidecar configured but not reachable; {keys_txt}",
        )
    kit = body.get("enrichmentKit") or {}
    version = f"{kit.get('version')}@{str(kit.get('commit'))[:10]}"
    prov = body.get("providers") or {}
    running = ", ".join(
        f"{p}={'configured' if (prov.get(p) or {}).get('configured') else 'missing'}"
        for p in ("hunter", "apollo")
    )
    return ComponentCheck(
        name="enrichment",
        state="ok" if any(keys.values()) else "warning",
        detail=f"sidecar running (enrichment-kit {version}; sidecar sees {running}); "
        f"app sees {keys_txt}",
    )


def build_readiness(settings: Settings, registry: ProviderRegistry) -> ReadinessResponse:
    database = _database_check(settings)
    checks = [
        ComponentCheck(name="configuration", state="ok", detail=f"env={settings.app_env.value}"),
        _provider_mode_check(settings),
        database,
        _schema_check(settings, database),
        _enrichment_check(settings),
    ]
    states = {c.state for c in checks}
    status: OverallStatus = (
        "error" if "error" in states else "degraded" if "warning" in states else "ok"
    )
    return ReadinessResponse(
        status=status,
        environment=settings.app_env.value,
        provider_mode=settings.provider_mode.value,
        paid_provider_calls_allowed=settings.allow_paid_provider_calls,
        checks=checks,
        providers=registry.statuses(),
    )
