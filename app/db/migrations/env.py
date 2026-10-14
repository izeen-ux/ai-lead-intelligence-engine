"""Alembic environment.

The database URL comes from application settings (``DATABASE_URL``), never from alembic.ini.
Programmatic callers (the test suite) may instead pass an explicit URL via
``config.attributes["database_url"]`` so migrations can target a separate test database.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

import app.db.models  # noqa: F401  (registers tables on Base.metadata)
from app.core.config import get_settings
from app.db.base import Base

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

database_url: str | None = config.attributes.get("database_url")
if database_url is None:
    settings = get_settings()
    if settings.database_url is None:
        raise RuntimeError("DATABASE_URL must be set to run migrations")
    database_url = settings.effective_database_url or settings.database_url.get_secret_value()
# Escape '%' so configparser interpolation does not mangle URL-encoded passwords.
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
