"""Lazy database engine/session factory and a connectivity check.

Nothing connects at import time. When ``DATABASE_URL`` is unset the app still starts and the
readiness check reports the database as ``not_configured``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.errors import ConfigurationError

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine(settings: Settings) -> Engine:
    global _engine, _session_factory
    if _engine is None:
        if settings.database_url is None:
            raise ConfigurationError("DATABASE_URL is not set")
        _engine = create_engine(
            settings.effective_database_url or settings.database_url.get_secret_value(),
            echo=settings.database_echo,
            pool_pre_ping=True,
            connect_args={"connect_timeout": settings.database_connect_timeout_seconds},
        )
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


@contextmanager
def session_scope(settings: Settings) -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on error."""
    get_engine(settings)
    if _session_factory is None:  # pragma: no cover - set by get_engine
        raise ConfigurationError("session factory not initialised")
    session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_database(settings: Settings) -> tuple[str, str | None]:
    """Return ``(state, detail)`` where state is ``not_configured`` | ``ok`` | ``error``.

    ``detail`` never includes the connection string (it may contain a password).
    """
    if not settings.database_configured:
        return "not_configured", "DATABASE_URL not set"
    try:
        with get_engine(settings).connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        return "error", type(exc).__name__
    return "ok", None


ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def expected_schema_revision() -> str | None:
    """Head revision of the migration scripts shipped with this code."""
    script = ScriptDirectory.from_config(AlembicConfig(str(ALEMBIC_INI)))
    return script.get_current_head()


def check_schema(settings: Settings) -> tuple[str, str | None]:
    """``ok`` when the database is at the migration head, ``outdated`` when behind/ahead."""
    if not settings.database_configured:
        return "not_configured", "DATABASE_URL not set"
    try:
        with get_engine(settings).connect() as conn:
            current = MigrationContext.configure(conn).get_current_revision()
    except Exception as exc:
        return "error", type(exc).__name__
    head = expected_schema_revision()
    if current != head:
        return "outdated", f"database at {current}, code expects {head}; run: alembic upgrade head"
    return "ok", f"revision {current}"


def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
