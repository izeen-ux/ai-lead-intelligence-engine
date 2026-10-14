from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from app import __version__
from app.api.deps import get_app_settings, get_registry
from app.core.config import Settings
from app.providers.registry import ProviderRegistry
from app.schemas.health import LivenessResponse, ReadinessResponse
from app.services.health import build_readiness

router = APIRouter(tags=["health"])


@router.get("/health", response_model=LivenessResponse)
def liveness(settings: Settings = Depends(get_app_settings)) -> LivenessResponse:
    """Process is up. Cheap, no dependencies checked."""
    return LivenessResponse(app=settings.app_name, version=__version__)


@router.get("/health/ready", response_model=ReadinessResponse)
def readiness(
    response: Response,
    settings: Settings = Depends(get_app_settings),
    registry: ProviderRegistry = Depends(get_registry),
) -> ReadinessResponse:
    """Configuration, database and provider-configuration status. Never calls a provider."""
    result = build_readiness(settings, registry)
    if result.status == "error":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return result
