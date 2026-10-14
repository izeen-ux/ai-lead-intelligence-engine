"""Prepare a copy of this project on ANOTHER laptop (run once, before the first start).

  uv run python scripts/setup_new_machine.py                     # check + prepare .env
  uv run python scripts/setup_new_machine.py --load-data backups/<file>.dump

Step 1 (always): checks that the files a copy needs are there and adds POSTGRES_PASSWORD to
``.env`` (taken from DATABASE_URL, never printed) - a brand-new PostgreSQL volume refuses to
start without it.

Step 2 (``--load-data``): loads the leads of the original laptop from a backup file into the
EMPTY database of this laptop. Refused when the database already holds data: it never
overwrites anything. Run it after ``docker compose up -d postgres`` and before the full start.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.utils.pg_backup import CONTAINER, database_identity, docker

ENV = Path(".env")
KEY_FILE = Path("secrets/google/service-account.json")


def prepare_env() -> list[str]:
    """Problems found (empty = ready). Adds POSTGRES_PASSWORD when it is missing."""
    problems = []
    if not ENV.is_file():
        return ["`.env` is missing - it must come with the project copy (it holds the keys)"]
    if shutil.which("docker") is None:
        problems.append("Docker is not installed or not on PATH - install Docker Desktop")
    if not KEY_FILE.is_file():
        problems.append(f"`{KEY_FILE.as_posix()}` is missing - the Google Sheet cannot be written")
    settings = Settings()
    if settings.database_url is None:
        return [*problems, "DATABASE_URL is not set in `.env`"]
    lines = ENV.read_text(encoding="utf-8").splitlines()
    if not any(
        line.startswith("POSTGRES_PASSWORD=") and line.strip() != "POSTGRES_PASSWORD="
        for line in lines
    ):
        password = make_url(settings.database_url.get_secret_value()).password or ""
        if not password:
            return [*problems, "DATABASE_URL has no password - cannot prepare the database"]
        kept = [line for line in lines if not line.startswith("POSTGRES_PASSWORD=")]
        kept += ["", "# password of the local PostgreSQL container (same as in DATABASE_URL)",
                 f"POSTGRES_PASSWORD={password}"]  # fmt: skip
        ENV.write_text("\n".join(kept) + "\n", encoding="utf-8")
        print("added POSTGRES_PASSWORD to .env (value taken from DATABASE_URL, not shown)")
    else:
        print("POSTGRES_PASSWORD already set in .env")
    if settings.operator_api_token is None:
        problems.append("OPERATOR_API_TOKEN is not set in `.env` - the dashboard will not start")
    return problems


def load_data(dump: Path) -> int:
    if not dump.is_file():
        print(f"REFUSING: backup file not found: {dump}")
        return 2
    database, user = database_identity(Settings())
    tables = docker(
        CONTAINER, "psql", "-U", user, "-d", database, "-Atc",
        "select count(*) from information_schema.tables where table_schema = 'public'",
    )  # fmt: skip
    if int(tables.decode().strip() or 0) > 0:
        print(
            f"REFUSING: database {database!r} already has tables. Data is only loaded into an "
            "empty database (nothing is ever overwritten)."
        )
        return 2
    docker(
        CONTAINER, "pg_restore", "-U", user, "-d", database, "--no-owner", "--exit-on-error",
        stdin=dump.read_bytes(),
    )  # fmt: skip
    companies = docker(
        CONTAINER, "psql", "-U", user, "-d", database, "-Atc", "select count(*) from companies"
    )
    version = docker(
        CONTAINER, "psql", "-U", user, "-d", database, "-Atc",
        "select version_num from alembic_version",
    )  # fmt: skip
    print(
        f"loaded {dump.name}: {companies.decode().strip()} companies, "
        f"schema version {version.decode().strip()}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare this project copy on a new laptop.")
    parser.add_argument("--load-data", type=Path, default=None, help="backup file (.dump)")
    args = parser.parse_args(argv)
    if args.load_data is not None:
        return load_data(args.load_data)
    problems = prepare_env()
    if problems:
        print("NOT READY:\n  - " + "\n  - ".join(problems))
        return 2
    print("READY: .env and key file are in place. Next: docker volume create lead-intel-pgdata")
    return 0


if __name__ == "__main__":
    sys.exit(main())
