"""Back up the PostgreSQL database (Phase 10).

Runs ``pg_dump`` (custom format, compressed) INSIDE the PostgreSQL container and stores the
file on this computer:  backups/<database>_<UTC timestamp>.dump   (the folder is git-ignored)

Then it verifies the file (``pg_restore --list`` must read it), prints its size and SHA-256,
and applies retention: only the newest N backups are kept (default 14, ``--keep``).
A backup contains lead and contact data - store copies only on encrypted storage.

A backup is only proven by a restore: run ``scripts/restore_db.py`` on the new file.

Examples:
  uv run python scripts/backup_db.py
  uv run python scripts/backup_db.py --keep 30
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import Settings
from app.utils.pg_backup import (
    BACKUP_DIR,
    CONTAINER,
    apply_retention,
    backup_name,
    database_identity,
    docker,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--container", default=CONTAINER, help="PostgreSQL container name")
    p.add_argument("--dir", type=Path, default=BACKUP_DIR, help="backup folder")
    p.add_argument("--keep", type=int, default=14, help="how many backups to keep (retention)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.keep < 1:
        print("REFUSING: --keep must be at least 1")
        return 2
    database, user = database_identity(Settings())
    args.dir.mkdir(parents=True, exist_ok=True)
    target = args.dir / backup_name(database, datetime.now(UTC))
    dump = docker(args.container, "pg_dump", "-U", user, "-d", database, "-Fc", "--no-owner")
    if len(dump) < 1024:
        raise SystemExit("backup is suspiciously small - not saved")
    target.write_bytes(dump)
    listing = docker(args.container, "pg_restore", "--list", stdin=dump).decode("utf-8", "replace")
    entries = [line for line in listing.splitlines() if line and not line.startswith(";")]
    tables = sum(1 for line in entries if " TABLE DATA " in line)
    removed = apply_retention(args.dir, database, args.keep)
    print(f"backup written: {target}")
    print(f"  size: {len(dump):,} bytes   sha256: {hashlib.sha256(dump).hexdigest()}")
    print(f"  readable by pg_restore: {len(entries)} objects, {tables} tables with data")
    print(f"  retention: keeping newest {args.keep}; removed {len(removed)} old backup(s)")
    print(f"next: uv run python scripts/restore_db.py --file {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
