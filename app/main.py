"""Application entrypoint (app factory).

Run locally:  ``uv run uvicorn app.main:create_app --factory --reload``

A factory (instead of a module-level ``app``) means importing this module never reads the
environment, so tests can build isolated apps with explicit settings.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.errors import register_error_handlers
from app.api.middleware import RequestContextMiddleware
from app.api.routes import dashboard, health
from app.api.security import HardeningMiddleware, cors_origins, enforce_production_profile
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import dispose_engine
from app.providers.registry import ProviderRegistry
from app.services.dashboard import DashboardBackend
from app.services.startup import run_startup_checks


def create_app(
    settings: Settings | None = None, *, dashboard_backend: DashboardBackend | None = None
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(
        level=settings.log_level,
        fmt=settings.log_format.value,
        secret_values=settings.secret_values(),
    )

    enforce_production_profile(settings)  # staging/production never start without operator auth
    origins = cors_origins(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        run_startup_checks(settings)
        yield
        # Graceful shutdown (SIGTERM/SIGINT): cancel in-flight pipeline runs so they are
        # recorded as CANCELLED instead of staying RUNNING until stale recovery.
        tasks: set[asyncio.Task[object]] = app.state.pipeline_tasks
        for task in list(tasks):
            task.cancel()
        for task in list(tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        dispose_engine()

    app = FastAPI(
        title="AI Lead Intelligence Engine",
        version=__version__,
        debug=settings.debug,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.registry = ProviderRegistry(settings)
    app.state.dashboard_backend = dashboard_backend  # None = PostgreSQL on first use
    app.state.pipeline_tasks = set()
    app.add_middleware(RequestContextMiddleware)
    if origins:  # explicit allow-list only; default = same-origin (no CORS headers at all)
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type", "X-Dashboard-Request"],
            allow_credentials=False,
        )
    app.add_middleware(HardeningMiddleware, max_body_bytes=settings.max_request_body_bytes)
    register_error_handlers(app)
    app.include_router(health.router)
    app.include_router(dashboard.page_router)
    app.include_router(dashboard.router)
    return app
