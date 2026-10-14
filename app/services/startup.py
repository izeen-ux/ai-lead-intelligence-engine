"""Startup checks: log a secret-free configuration summary and warn about risky switches."""

from __future__ import annotations

from app.core.config import ProviderMode, Settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def run_startup_checks(settings: Settings) -> list[str]:
    """Returns warnings (also logged). Never performs network I/O."""
    warnings: list[str] = []
    if settings.provider_mode is ProviderMode.LIVE:
        warnings.append("PROVIDER_MODE=live: real provider adapters are active for this process")
        missing = [p.value for p in settings.missing_provider_credentials()]
        if missing:
            warnings.append("missing provider credentials: " + ", ".join(missing))
    if settings.allow_paid_provider_calls:
        warnings.append("ALLOW_PAID_PROVIDER_CALLS=true: paid provider credits can be spent")
    if not settings.database_configured:
        warnings.append("DATABASE_URL not set: running without persistence")

    logger.info(
        "startup configuration",
        extra={
            "environment": settings.app_env.value,
            "provider_mode": settings.provider_mode.value,
            "paid_calls_allowed": settings.allow_paid_provider_calls,
            "database_configured": settings.database_configured,
        },
    )
    for w in warnings:
        logger.warning(w)
    return warnings
