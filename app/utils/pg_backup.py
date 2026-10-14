"""Shared helpers for the PostgreSQL backup / restore scripts (Phase 10).

Everything runs inside the PostgreSQL container over its local socket, so no database password
is passed on a command line or printed.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.engine import make_url

from app.core.config import Settings

CONTAINER = "lead-intel-pg"
BACKUP_DIR = Path("backups")


def database_identity(settings: Settings) -> tuple[str, str]:
    """(database name, user) from DATABASE_URL. The password is never used or printed."""
    if settings.database_url is None:
        raise SystemExit("DATABASE_URL is not set")
    url = make_url(settings.database_url.get_secret_value())
    if not url.database or not url.username:
        raise SystemExit("DATABASE_URL must contain a database name and a user")
    return url.database, url.username


def docker(container: str, *args: str, stdin: bytes | None = None) -> bytes:
    """Run a command in the container (local socket: no password leaves the container)."""
    cmd = ["docker", "exec", *(["-i"] if stdin is not None else []), container, *args]
    done = subprocess.run(cmd, input=stdin, capture_output=True, check=False)  # noqa: S603
    if done.returncode != 0:
        message = done.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["failed"]
        raise SystemExit(f"{args[0]} failed: {message[0]}")
    return done.stdout


def backup_name(database: str, now: datetime) -> str:
    return f"{database}_{now.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}.dump"


def apply_retention(folder: Path, database: str, keep: int) -> list[Path]:
    """Delete the oldest backups of this database beyond ``keep``; returns what was removed."""
    files = sorted(folder.glob(f"{database}_*.dump"), key=lambda f: f.name, reverse=True)
    removed = files[max(keep, 1) :]
    for old in removed:
        old.unlink()
    return removed
