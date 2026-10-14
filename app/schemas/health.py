"""API response DTOs for health endpoints (kept separate from domain models)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from app.providers.contracts import ProviderStatus

OverallStatus = Literal["ok", "degraded", "error"]


class LivenessResponse(BaseModel):
    status: Literal["ok"] = "ok"
    app: str
    version: str


class ComponentCheck(BaseModel):
    name: str
    state: Literal["ok", "not_configured", "error", "warning"]
    detail: str | None = None


class ReadinessResponse(BaseModel):
    status: OverallStatus
    environment: str
    provider_mode: str
    paid_provider_calls_allowed: bool
    checks: list[ComponentCheck]
    providers: list[ProviderStatus]
