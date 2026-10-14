# Production Hardening Policy (Phase 10)

The rules the release candidate is built to. Details and evidence:
[../SECURITY_REVIEW.md](../SECURITY_REVIEW.md), [../DEPLOYMENT_RUNBOOK.md](../DEPLOYMENT_RUNBOOK.md),
[../BACKUP_AND_RESTORE.md](../BACKUP_AND_RESTORE.md).

## 1. Safe by default

`PROVIDER_MODE=mock`, `ALLOW_PAID_PROVIDER_CALLS=false`, `ALLOW_SHEETS_WRITES=false` are the
permanent baseline in `.env`. Live mode, paid calls and Sheet writes are enabled **per
process** (one command / one `docker compose up`) and nothing in the code turns them on.

## 2. Secrets

- Only in the environment (`.env`, git-ignored) or a mounted file (`secrets/google`).
- Never in source, images, logs, error messages, API responses, the dashboard or docs.
- `.env.example` lists every setting with empty secret values (enforced by a test).
- Each paid provider has a documented rotation procedure. Use separate, restricted keys for a
  production server where the provider supports it.

## 3. Packaging

- Images build only from the lockfile (`uv sync --frozen --no-dev`), run as non-root with a
  read-only filesystem, no capabilities, `no-new-privileges`.
- One supported start/stop path: `docker compose up -d` / `docker compose down`.
- Health: `/health` (liveness), `/health/ready` (database, schema, sidecar, configuration) —
  neither calls a paid provider. Logs go to stdout as JSON.
- SIGTERM/SIGINT: open requests finish, a running pipeline is cancelled and recorded.

## 4. Network

- Published ports bind to loopback only. Laya and enrichment-kit are private.
- Sidecar endpoints must be loopback or listed in `INTERNAL_SERVICE_HOSTS`.
- CORS off unless exact origins are configured. Bodies ≤ 64 KB.
- Crawl targets must be public DNS names.
- Remote access = HTTPS reverse proxy in front of `127.0.0.1:8000`; bindings stay as they are.

## 5. Authorization

| Surface | Rule |
|---|---|
| `/health`, `/health/ready`, `/dashboard` (static) | open; no data, no secrets |
| `/api/v1/*` (reads) | operator token |
| review action, pipeline trigger, pipeline recovery | operator token + write header; actor name recorded |
| live runs, Sheet writes, backups, restores, configuration | access to the machine and `.env` |

Staging/production refuse to start without an operator token of 24+ characters.

## 6. Database

- PostgreSQL is the system of record. History tables are append-only (database triggers).
- Migrations are forward-only and run as an explicit step (`migrate` service). No automatic
  reset, ever. The data volume is external to Compose.
- Back up before every live run and upgrade; a backup counts only after a restore test.

## 7. Reliability

- Every outbound call has a timeout and bounded retries for transient errors only.
- Provider failure never bypasses a gate: stages fail closed; the pipeline stops.
- One pipeline at a time; stale runs are recovered; exports are idempotent.
- Budgets at four levels: per call (AI worst case), per stage run, per month, per pipeline run.

## 8. AI provider

OpenAI through the `ReasoningProvider` boundary only; strict structured output; the domain
validates every answer; no tools; the model name, usage and estimated cost are stored with each
analysis. The Techno-AI-Router is deprecated and opt-in only; historical records are untouched.

## 9. Observability

Structured logs carry timestamp, level, request id, pipeline run id, stage, provider, status,
error code, duration and safe usage/cost. The dashboard answers: alive? ready? pipeline
running? last run? which stage/provider failed? spend? leads produced? exported?

## 10. Demo data

Synthetic data lives in memory or in a database named `lead_intel_demo…`, is shown as such in
the dashboard header, and can only be exported to `FIXTURE_` Sheet tabs. The ICP is never
relaxed and no decision is forged for a demo.
