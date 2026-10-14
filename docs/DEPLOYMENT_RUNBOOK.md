# Deployment Runbook

How to install the release candidate on a machine (the current laptop, another PC or a single
staging server). Target shape: **one machine, Docker Compose**. No Kubernetes, no cloud
service is required. A public-cloud deployment is not part of this release.

```text
                127.0.0.1:8000
browser ───────────────► app (FastAPI: API + dashboard)        scheduler (pipeline ticks)
                              │                                     │
        private Compose network (no ports published) ───────────────┤
          ├── postgres   (also 127.0.0.1:5432 for host tools)       │
          ├── crawl4ai   (also 127.0.0.1:11235)                     │
          ├── laya                                                  │
          └── enrichment-kit                                        │
outbound HTTPS only: Apify, Firecrawl, Hunter, Apollo, OpenAI, Google Sheets
```

## 1. Requirements

- Docker Desktop (Windows/macOS) or Docker Engine + Compose v2 (Linux), 8 GB RAM free
  (Laya image 1.7 GB, CPU only).
- For the helper commands (`uv run python scripts/...`): Python 3.13 + `uv` on the host.
- Git checkout of this repository.

## 2. Files that are NOT in the repository (create them on the machine)

| File | Content |
|---|---|
| `.env` | copy `.env.example`, fill the secrets (list below). Keep `PROVIDER_MODE=mock`, `ALLOW_PAID_PROVIDER_CALLS=false`, `ALLOW_SHEETS_WRITES=false` |
| `secrets/google/service-account.json` | the Google service-account key file |

Secrets to fill in `.env`:

| Variable | From |
|---|---|
| `DATABASE_URL` | `postgresql+psycopg://postgres:<password>@localhost:5432/lead_intel` — you choose the password |
| `POSTGRES_PASSWORD` | the same password (only needed the first time, when the data volume is empty) |
| `OPERATOR_API_TOKEN` | any random string of 24+ characters (`python -c "import secrets; print(secrets.token_urlsafe(36))"`) |
| `ENRICHMENT_KIT_API_TOKEN` | another random string (local token between app and sidecar) |
| `OPENAI_API_KEY` | OpenAI dashboard ([OPENAI_PROVIDER_NOTES.md](OPENAI_PROVIDER_NOTES.md)) |
| `APIFY_TOKEN`, `FIRECRAWL_API_KEY`, `HUNTER_API_KEY`, `APOLLO_API_KEY` | provider dashboards ([providers/PROVIDER_SETUP.md](providers/PROVIDER_SETUP.md)) |
| `CRAWL4AI_API_TOKEN` | random string |
| `GOOGLE_SHEETS_SPREADSHEET_ID` | the Sheet's id ([GOOGLE_SHEETS_SETUP_GUIDE.md](GOOGLE_SHEETS_SETUP_GUIDE.md)) |

On a real server prefer the platform's secret store over a file; the app only reads
environment variables, so any mechanism that sets them works. Nothing secret is ever built
into an image (`.dockerignore` excludes `.env` and `secrets/`).

## 3. First start

```powershell
docker volume create lead-intel-pgdata      # the data volume (Compose never deletes it)
docker compose up -d --build                # builds 3 images, starts 6 services, runs migrations
docker compose ps
curl.exe http://127.0.0.1:8000/health/ready
```

What Compose does: `postgres` → healthy → `migrate` runs `alembic upgrade head`
(forward-only, never resets data) → `app` and `scheduler` start. Images are built from
`uv.lock` (`uv sync --frozen --no-dev`) and run as non-root users with a read-only filesystem,
no Linux capabilities and `no-new-privileges`.

## 4. Upgrade to a new version

```powershell
uv run python scripts/backup_db.py          # always back up first
git pull
docker compose up -d --build                # rebuild, migrate, restart
docker compose ps
curl.exe http://127.0.0.1:8000/health/ready
uv run python scripts/smoke_check.py
```

`docker compose up -d --build` is required after every code change: the `migrate` service
brings the database to the current head (**0010** since the lead-output expansion - two
additive phone columns, no data rewritten) and one-off `docker compose run ... app ...`
commands use the image, not the files on disk.

Roll back: `git checkout <previous commit>` + `docker compose up -d --build`. If a migration
already changed the schema and the old code cannot run with it, restore the backup
([BACKUP_AND_RESTORE.md](BACKUP_AND_RESTORE.md) §5).

## 5. Start / stop / restart

| Action | Command |
|---|---|
| start everything | `docker compose up -d` |
| stop everything (data kept) | `docker compose down` — never add `-v` |
| restart app + scheduler | `docker compose restart app scheduler` |
| logs | `docker compose logs -f app scheduler` |
| status | `docker compose ps` |

All services have `restart: unless-stopped`, so they come back after a reboot once Docker
starts. Stopping sends SIGTERM: the app finishes open requests, cancels a running pipeline
(recorded as `cancelled`) and exits within the 30 s grace period.

## 6. Network exposure (verify after every change of the Compose file)

```powershell
docker compose ps --format "table {{.Service}}\t{{.Ports}}"
netstat -ano | Select-String "LISTENING" | Select-String ":5432 |:8000 |:11235 |:8765 |:8787 "
```

Expected: `app 127.0.0.1:8000`, `postgres 127.0.0.1:5432` and `[::1]:5432`,
`crawl4ai 127.0.0.1:11235`; **nothing** for 8765 (Laya) and 8787 (enrichment-kit); no line
with `0.0.0.0`.

To let colleagues use the dashboard from other machines, do **not** change the bindings.
Put a reverse proxy with HTTPS in front of `127.0.0.1:8000` (for example Caddy or nginx on the
same machine) and, if the dashboard is served from another origin, list that exact origin in
`CORS_ALLOWED_ORIGINS`. Every API call still needs the operator token.

## 7. Production profile

Compose sets `APP_ENV=staging` for the app: the app refuses to start without
`OPERATOR_API_TOKEN` (24+ characters) and `DATABASE_URL`. Use `APP_ENV=production` for a real
server (same rules). Local development outside Docker (`uv run python scripts/run_dashboard.py`)
uses `APP_ENV=development`: without a token only this computer (loopback) may call the API.

## 8. Migration from the pre-Phase-10 setup on this laptop (done 2026-10-05)

The old manually created containers were replaced by the Compose services with the same
names; the database volume `lead-intel-pgdata` was reused (no data copied). The old
containers are kept, stopped, as a fallback:

```powershell
docker ps -a --filter name=old        # lead-intel-pg-old-0.0.0.0, lead-intel-crawl4ai-old
docker rm lead-intel-pg-old-0.0.0.0 lead-intel-crawl4ai-old     # delete once you are confident
```

Do not start `lead-intel-pg-old-0.0.0.0` while the stack is running: two PostgreSQL servers
must never use the same data volume at the same time.

## 9. Acceptance checklist after a deployment

1. `docker compose ps` – six services up/healthy, `migrate` exited (0)
2. `/health` and `/health/ready` → ok
3. `uv run python scripts/smoke_check.py` → `SMOKE CHECK PASSED`
4. `uv run python scripts/check_openai.py` → model available
5. dashboard opens, token accepted, a dry-run pipeline succeeds
   (`uv run python scripts/run_pipeline.py --db --stages all --limit 10`)
6. `uv run python scripts/backup_db.py` + `restore_db.py` → `VERIFICATION PASSED`
7. network check of §6
