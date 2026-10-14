# Orchestration and Operations Policy (Phase 9, minimal)

Policy version **`p9-2026.10.05`** (stored on every pipeline run).

## 1. What the orchestrator is — and is not

A pipeline run executes the existing stage services in a fixed order:

`discovery → website → signals → qualification → enrichment → analysis → export`

It adds sequencing, limits, history and recovery. It adds **no business rule** and can skip no
gate: every stage is the same service its own CLI uses, with its own gate, budget and
append-only history. Stage records link back through `stage_run_id`.

## 2. Modes

| Mode | Who can start it | What is written |
|---|---|---|
| **Dry run** (default: CLI, API, dashboard, scheduler in mock mode) | anyone with operator access | only the `pipeline_runs` row. Qualification, enrichment, analysis and export run read-only with mock providers; discovery, website and signals are **skipped** (they exist only to write) |
| **Writing run** (`--live` / `dry_run: false`) | only a process in `PROVIDER_MODE=live` | every stage persists. Paid stages additionally need `ALLOW_PAID_PROVIDER_CALLS=true`; the Sheet is written only with `ALLOW_SHEETS_WRITES=true` |

## 3. Which companies a stage picks (no repeat work, no repeat spend)

| Stage | Candidates |
|---|---|
| discovery | only when `discovery` is in `PIPELINE_STAGES` **and** `PIPELINE_DISCOVERY_KEYWORDS` is set (source `funding_news`, India) |
| website | companies with an unresolved domain |
| signals | VERIFIED companies never researched |
| qualification | VERIFIED + researched companies with no decision, or researched again after their last **automated** decision. A decision made by a human review is never re-decided by the pipeline |
| enrichment | latest decision QUALIFIED and no VERIFIED contact under that decision |
| analysis | QUALIFIED + VERIFIED contact and no accepted analysis for that decision |
| export | all QUALIFIED leads / REVIEW cases (idempotent upsert; unchanged rows are not written) |

At most `PIPELINE_COMPANY_LIMIT` (5) companies per stage per run.

## 4. Fail-closed rules

- A stage that fails stops the run; later stages do not run on incomplete input.
- Before a writing stage uses a local sidecar or the AI provider, its free health/model check
  must pass (Laya `/health`, enrichment-kit `/health`, OpenAI `models.retrieve`). A sidecar
  that is down fails the stage instead of producing degraded results.
- If the summed estimated cost exceeds `PIPELINE_MAX_ESTIMATED_COST_USD_PER_RUN` (1.00) the run
  stops with `pipeline_budget_exhausted`. Stage budgets (Apify, Firecrawl, Hunter/Apollo,
  OpenAI) apply underneath.
- An unexpected exception fails the stage with `internal_error`; details stay in the server
  log, never in the API response.

## 5. One run at a time

`pipeline_runs.active_slot` is 1 only while a run is RUNNING and is unique: a second trigger
(CLI, API, scheduler) is refused (`409` / exit code 3). The scheduler skips its tick.

## 6. Status, heartbeat, recovery

Statuses: `running`, `succeeded`, `failed`, `cancelled` (process stopped: SIGTERM/Ctrl+C —
recorded before exit), `abandoned` (stale). The heartbeat is updated after every stage. A
RUNNING run without a heartbeat for `PIPELINE_STALE_AFTER_MINUTES` (60) is closed by recovery
(`--recover`, dashboard button, and automatically by the scheduler every minute). Recovery
never touches stage data; the next run continues from the candidate rules in §3.

## 7. Scheduler

`scripts/run_scheduler.py` (Compose service `scheduler`): every 60 s recover stale runs; when
`PIPELINE_SCHEDULE_INTERVAL_MINUTES > 0` and no run is active, start one — a writing run only
if that process is in live mode, otherwise a dry run. It never changes the safety switches.

## 8. Operator surface

| Action | CLI | API |
|---|---|---|
| run | `scripts/run_pipeline.py [--db] [--live] [--stages a,b] [--limit N]` | `POST /api/v1/pipeline/runs` |
| list / detail | `--db --status` | `GET /api/v1/pipeline/runs`, `/runs/{id}` |
| recover | `--db --recover` | `POST /api/v1/pipeline/recover` |

## 9. Deliberately not included (minimal Phase 9)

Per-stage automatic retries, parallel workers / queues, alerting (email/Slack), retention
jobs, outreach. The run history and logs carry enough to add them later.
