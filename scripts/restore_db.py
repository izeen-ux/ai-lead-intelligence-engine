"""Restore a backup into a SEPARATE database and verify it (Phase 10).

This never touches the live database: the target name must start with ``lead_intel_restore``
and must differ from the database in DATABASE_URL. The target is dropped and re-created, the
backup is restored into it, and the result is compared with the live database:

  * migration version (alembic_version) is the same
  * every table in the backup exists and has the SAME row count as the live table
    (run it right after a backup, before new pipeline runs, for an exact match)

To really recover from a disaster, see docs/BACKUP_AND_RESTORE.md (it uses the same commands
with the real database name, after an explicit confirmation step).

Examples:
  uv run python scripts/restore_db.py --file backups/lead_intel_20261005T101500Z.dump
  uv run python scripts/restore_db.py --file <file> --drop-after
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from app.core.config import Settings
from app.utils.pg_backup import CONTAINER, database_identity, docker

TARGET_PREFIX = "lead_intel_restore"
SAFE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--file", type=Path, required=True, help="backup file (.dump)")
    p.add_argument("--target-db", default=f"{TARGET_PREFIX}_check", help="scratch database name")
    p.add_argument("--container", default=CONTAINER, help="PostgreSQL container name")
    p.add_argument("--drop-after", action="store_true", help="drop the scratch database at the end")
    return p.parse_args(argv)


def target_problem(target: str, live: str) -> str | None:
    if not SAFE_NAME.match(target):
        return "target database name must be lowercase letters, digits and underscores"
    if target == live:
        return "the target must NOT be the live database"
    if not target.startswith(TARGET_PREFIX):
        return f"the target database name must start with {TARGET_PREFIX!r}"
    return None


def psql(container: str, user: str, database: str, sql: str) -> list[str]:
    out = docker(container, "psql", "-U", user, "-d", database, "-At", "-c", sql)
    return [line for line in out.decode("utf-8", "replace").splitlines() if line]


def row_counts(container: str, user: str, database: str) -> dict[str, int]:
    tables = psql(
        container, user, database,
        "select tablename from pg_tables where schemaname = 'public' order by tablename",
    )  # fmt: skip
    counts = {}
    for table in tables:
        if SAFE_NAME.match(table):  # names come from the catalog; still validated before use
            counts[table] = int(
                psql(container, user, database, f'select count(*) from "{table}"')[0]  # noqa: S608
            )
    return counts


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    live, user = database_identity(Settings())
    problem = target_problem(args.target_db, live)
    if problem:
        print(f"REFUSING: {problem}")
        return 2
    if not args.file.is_file():
        print(f"REFUSING: backup file not found: {args.file}")
        return 2
    dump = args.file.read_bytes()
    target = args.target_db
    docker(args.container, "dropdb", "-U", user, "--if-exists", target)
    docker(args.container, "createdb", "-U", user, target)
    docker(
        args.container, "pg_restore", "-U", user, "-d", target, "--no-owner", "--exit-on-error",
        stdin=dump,
    )  # fmt: skip
    restored = row_counts(args.container, user, target)
    current = row_counts(args.container, user, live)
    version = psql(args.container, user, target, "select version_num from alembic_version")
    live_version = psql(args.container, user, live, "select version_num from alembic_version")
    triggers = psql(
        args.container, user, target,
        "select count(*) from pg_trigger where not tgisinternal",
    )  # fmt: skip
    missing = sorted(set(current) - set(restored))
    different = {t: (restored[t], current[t]) for t in restored if current.get(t) != restored[t]}

    print(f"restored {args.file} into database {target!r} (live database {live!r} untouched)")
    print(f"  migration version: restored {version[0]} | live {live_version[0]}")
    print(f"  tables: {len(restored)}   rows: {sum(restored.values()):,}   "
          f"append-only triggers: {triggers[0]}")  # fmt: skip
    for table, rows in restored.items():
        print(f"    {table:<28} {rows:>8}")
    ok = not missing and not different and version == live_version
    if missing:
        print(f"  MISSING tables: {missing}")
    if different:
        print(f"  ROW COUNT differs from live (restored, live): {different}")
    print("VERIFICATION " + ("PASSED" if ok else "FAILED (see above)"))
    if args.drop_after:
        docker(args.container, "dropdb", "-U", user, "--if-exists", target)
        print(f"scratch database {target!r} dropped")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
