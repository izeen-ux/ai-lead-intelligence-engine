# Backup and Restore (PostgreSQL)

PostgreSQL is the only place the business data lives (companies, signals, decisions, contacts,
analyses, review audit, export and pipeline history). The Google Sheet and the dashboard can be
rebuilt from it; the database cannot be rebuilt from them.

## 1. Make a backup

```powershell
uv run python scripts/backup_db.py
```

- Runs `pg_dump` (custom format, compressed) inside the `lead-intel-pg` container.
- Writes `backups\lead_intel_<UTC date>T<UTC time>Z.dump` (the `backups\` folder is git-ignored).
- Verifies the file can be read by `pg_restore`, prints size and SHA-256.
- Retention: keeps the newest **14** backups and deletes older ones (`--keep 30` to change).

A backup contains contact names and work emails. Keep copies only on an encrypted disk
(BitLocker) or an encrypted cloud folder. Never commit a backup.

## 2. Prove the backup works (restore test)

```powershell
uv run python scripts/restore_db.py --file backups\lead_intel_<...>.dump
```

This restores into a **separate** database `lead_intel_restore_check` (it refuses any target
that is the live database or does not start with `lead_intel_restore`), then compares it with
the live database: same migration version, same tables, same row counts. It ends with
`VERIFICATION PASSED` or lists the differences. Add `--drop-after` to remove the scratch
database when done.

Run it right after the backup (before new pipeline runs) for an exact row-count match.

## 3. Recorded restore test (2026-10-05)

| Item | Result |
|---|---|
| Backup file | `backups\lead_intel_20261004T202603Z.dump` (UTC timestamp) |
| Size / SHA-256 | 117,499 bytes / `a1739a8cb139e3919799ee97f90f063572ff2217e61a9052462a6b2bf71bcadf` |
| Readable by `pg_restore` | 164 objects, 23 tables with data |
| Restored into | `lead_intel_restore_check` (live `lead_intel` untouched) |
| Migration version | restored `0009` = live `0009` |
| Tables / rows | 23 tables, 108 rows — every table equal to live |
| Append-only triggers restored | 6 (decisions, contacts, attempts, analyses, export items, human reviews) |
| Result | **VERIFICATION PASSED** |

## 4. How often, and what you can lose (RPO / RTO)

| | Assumption for this release |
|---|---|
| Schedule | **before every live pipeline run and at least once a day** on days the system is used (Windows Task Scheduler can run the backup command; it is one line) |
| RPO (data you may lose) | everything since the last backup — at most 24 h with the daily habit, zero for a run you backed up before |
| RTO (time to be running again) | about 15 minutes: start Docker (2 min), `docker compose up -d` (3 min), restore (under 1 min at the current size), check (5 min) |
| Retention | 14 newest files locally; copy one per week to a second, encrypted location |
| What is lost with a backup | nothing structural: tables, constraints and the append-only triggers are in the dump |

The lost work is also re-creatable in part: discovery/crawl/signal data can be re-collected by
running the pipeline again (at provider cost); human review decisions and the exact history
cannot — hence the backup before live runs.

## 5. Real disaster recovery (replace the live database)

Only when the live database is lost or damaged. Read all steps first.

```powershell
docker compose stop app scheduler                                   # 1. nothing writes any more
uv run python scripts/restore_db.py --file backups\<newest good file>   # 2. prove the file is good
docker exec lead-intel-pg psql -U postgres -c "ALTER DATABASE lead_intel RENAME TO lead_intel_broken"
docker exec lead-intel-pg psql -U postgres -c "ALTER DATABASE lead_intel_restore_check RENAME TO lead_intel"
docker compose up -d                                                # 3. migrate + start
curl.exe http://127.0.0.1:8000/health/ready                         # 4. must be ok
```

Step 2 already restored the backup into `lead_intel_restore_check`; the two rename commands
swap it in and keep the damaged database as `lead_intel_broken` (delete it later with
`docker exec lead-intel-pg dropdb -U postgres lead_intel_broken` once you are sure).

If the whole Docker volume is gone: `docker volume create lead-intel-pgdata`, add
`POSTGRES_PASSWORD=<the password used in DATABASE_URL>` to `.env`, run
`docker compose up -d postgres` (this creates an empty `lead_intel`), then follow steps 2–4
above.

## 6. Rules

- The application never resets or drops the database. Compose runs `alembic upgrade head`
  only (forward-only). `docker compose down` keeps the data; **never** add `-v`.
- Before any manual schema change or a migration marked destructive: back up first.
- The data volume `lead-intel-pgdata` is declared `external` in Compose so Compose cannot
  delete it.
