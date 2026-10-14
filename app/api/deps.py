"""FastAPI dependencies. Overridable in tests via ``app.dependency_overrides``."""

from __future__ import annotations

from fastapi import Request

from app.core.config import Settings
from app.db.session import get_engine
from app.providers.registry import ProviderRegistry
from app.repositories.analysis import SqlAnalysisStore
from app.repositories.enrichment import SqlEnrichmentStore
from app.repositories.outputs import SqlOutputStore
from app.repositories.pipeline import SqlPipelineStore
from app.repositories.qualification import SqlQualificationStore
from app.services.credits import CreditService
from app.services.dashboard import DashboardBackend
from app.services.leads import LeadViewService
from app.services.pipeline import PipelineService
from app.services.pipeline_stages import sql_stages
from app.services.sheets_export import DEMO_DB_PREFIX


def get_app_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_registry(request: Request) -> ProviderRegistry:
    registry: ProviderRegistry = request.app.state.registry
    return registry


def sql_backend(settings: Settings, registry: ProviderRegistry) -> DashboardBackend:
    engine = get_engine(settings)
    qualifications = SqlQualificationStore(engine)
    views = LeadViewService(qualifications, SqlEnrichmentStore(engine), SqlAnalysisStore(engine))
    outputs = SqlOutputStore(engine)
    credits = CreditService(
        settings, outputs, hunter_usage=getattr(registry.enrichment, "account_usage", None)
    )
    pipeline = PipelineService(
        SqlPipelineStore(engine),
        sql_stages(settings, engine, registry),
        settings,
        credit_snapshot=credits.lines,
    )
    return DashboardBackend(
        outputs,
        views,
        qualifications,
        f"postgresql:{settings.database_name}",
        pipeline=pipeline,
        synthetic=(settings.database_name or "").startswith(DEMO_DB_PREFIX),
    )


def get_dashboard_backend(request: Request) -> DashboardBackend:
    """PostgreSQL by default (built on first use); a demo backend can be injected instead."""
    backend: DashboardBackend | None = getattr(request.app.state, "dashboard_backend", None)
    if backend is None:
        backend = sql_backend(get_app_settings(request), get_registry(request))
        request.app.state.dashboard_backend = backend
    return backend
