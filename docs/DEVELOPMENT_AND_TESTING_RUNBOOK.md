# Development & Testing Runbook (beginner-friendly)

**Last verified:** 2026-09-29 (end of Phase 4), on Windows 11 + PowerShell + Docker Desktop.
Every command below was run against this repository.

## 1. What this project does (in plain words)

It finds startups that recently raised money (Phase 2, Apify), finds and reads their official
website (Phase 3, Firecrawl with a Crawl4AI fallback), and collects public news/video signals
about them such as funding, hiring, expansion and AI interest (Phase 4, Agent-Reach). Everything
is stored in a PostgreSQL database with the source link for every fact. Later phases will score
the leads, enrich contacts and export them.

**Safety first:** by default everything runs in **mock / dry-run** mode - no internet, no cost.
Real calls only happen when you deliberately flip switches (section 9).

## 2. Open the project folder

```powershell
cd "E:\MY PROJECT'S\AI_Lead_Intelligence"
```
The quotes are needed because the path contains a space and an apostrophe.

## 3. Start the services (Docker Compose)

Start Docker Desktop first (wait until it says "Engine running"). Then, in the project folder:

```powershell
docker compose up -d          # database, Crawl4AI, Laya, enrichment sidecar, app, scheduler
docker compose ps             # all healthy? (migrate = "exited (0)" is normal)
docker compose down           # stop everything; the data stays
```

For tests and the helper scripts only the database is needed: `docker compose up -d postgres`.
All services restart by themselves after a reboot once Docker is running. The full operating
procedure is in `docs/OPERATOR_GUIDE.md`.

## 4. Check container status and logs

```powershell
docker ps                                  # running containers + ports
docker ps -a                               # also stopped ones ("Exited" = not running)
docker logs --tail 50 lead-intel-pg        # last 50 database log lines
docker logs --tail 50 lead-intel-crawl4ai  # last 50 crawler log lines
curl.exe http://127.0.0.1:11235/health     # crawler health: {"status":"ok","version":"0.9.4"}
```

Don't panic about two things you will see:
- Windows PowerShell 5.1 prints `docker logs` output in **red** (it treats the tool's stderr as
  an "error"). The text itself is what matters.
- The database log contains lines like `ERROR: duplicate key ... Key (domain)=(x.io) already
  exists`. Those come from the test suite, which deliberately checks that the database rejects
  duplicates (in the separate `lead_intel_test` database).

Security note: `lead-intel-pg` currently publishes port 5432 on all network interfaces
(`0.0.0.0`). To restrict it to this PC you would recreate the container with
`-p 127.0.0.1:5432:5432` (same volume `lead-intel-pgdata`, so data is kept). This was left
unchanged on purpose - do it when convenient.

## 5. Install / update Python dependencies

```powershell
uv sync
```
Creates/updates `.venv` exactly as pinned in `uv.lock`. If uv prints a "hardlink" warning,
it is harmless (you can silence it with `$env:UV_LINK_MODE="copy"`).

## 6. Database migrations (Alembic)

```powershell
uv run alembic current     # which schema version the DB is at (should say 0004 (head))
uv run alembic upgrade head  # apply any new migrations
uv run alembic check       # "No new upgrade operations detected" = code and DB agree
```

## 7. Tests, lint, type checks, smoke check

```powershell
uv run pytest -W error::DeprecationWarning --cov=app   # all tests (359 at end of Phase 4)
uv run pytest -m postgres                              # only the real-database tests
uv run ruff check .                                    # lint
uv run ruff format --check .                           # formatting
uv run mypy app tests scripts                          # strict type check
uv run python scripts/smoke_check.py                   # boots the app, proves no internet use
```
Tests never touch the internet (blocked automatically) and use a separate database
`lead_intel_test`, never your real data. If PostgreSQL is not running, the database tests are
**skipped** (within ~3 s) instead of hanging.

## 8. Start the API and check health

```powershell
uv run uvicorn app.main:create_app --factory --reload
```
In a **second** PowerShell window (use `curl.exe`, not `curl`, which is a PowerShell alias):
```powershell
curl.exe http://127.0.0.1:8000/health         # {"status":"ok",...}
curl.exe http://127.0.0.1:8000/health/ready   # status ok + database/schema checks
```
Stop the API with `Ctrl+C` in the first window.

## 9. Dry runs vs live runs (and which switches can spend money)

| Command | Dry run (default) | Live run needs | Can cost money? |
|---|---|---|---|
| `uv run python scripts/run_discovery.py --limit 5` | mock Apify, in-memory | `--live` + `PROVIDER_MODE=live` + `ALLOW_PAID_PROVIDER_CALLS=true` | **Yes** - Apify (~$0.0055 per 1-page run) |
| `uv run python scripts/run_website_intelligence.py` | mock Firecrawl/Crawl4AI | `--live` + `PROVIDER_MODE=live` + `ALLOW_PAID_PROVIDER_CALLS=true` (Firecrawl) | **Yes** - Firecrawl credits (≈ 6–11 per company) |
| `uv run python scripts/run_signal_research.py` | mock Agent-Reach, demo companies | `--live` + `PROVIDER_MODE=live` | No (public free sources), but it uses the internet |
| `uv run python scripts/run_qualification.py` | mock Laya, 3 demo companies, nothing written | `--db` (real data, read-only), `--persist` (write decisions), `--live` + `PROVIDER_MODE=live` + `LAYA_ENDPOINT_URL` (local Laya) | No - Laya runs on your PC; never calls Apify/Firecrawl/Hunter/Apollo |
| `uv run python scripts/evaluate_qualification.py` | mock Laya on the labeled (synthetic) set | `--live` (local Laya) | No |
| `uv run python scripts/run_enrichment.py` | mock Hunter/Apollo, 3 demo companies, nothing written | `--db --live` + `PROVIDER_MODE=live` + `ALLOW_PAID_PROVIDER_CALLS=true` + running sidecar | **Yes** - Hunter/Apollo credits (budget-capped) |

The two switches live in `.env` and should stay `PROVIDER_MODE=mock` and
`ALLOW_PAID_PROVIDER_CALLS=false`. For a one-off live run, set them **only for that command**:

```powershell
$env:PROVIDER_MODE="live"
uv run python scripts/run_signal_research.py --live --limit 1 --max-queries 2 --max-items 5
Remove-Item Env:PROVIDER_MODE
```
For paid runs add `$env:ALLOW_PAID_PROVIDER_CALLS="true"` and remove it afterwards
(`Remove-Item Env:ALLOW_PAID_PROVIDER_CALLS`). Budgets (pages, credits, queries, items) are
capped in settings; command-line flags can only lower them.

## 10. Phase 4 research - what it will and won't do

- Researches **only VERIFIED companies** (today: Drivn). Others are recorded as `skipped`.
- Uses only public, logged-out sources: Google News RSS search and YouTube metadata (yt-dlp,
  cookies disabled). LinkedIn, Instagram, X, Reddit, Facebook and anything needing a login or
  cookie are impossible to enable.
- Default budget per company: at most 5 queries and 20 processed items.
- Every stored signal has: source URL, platform (e.g. `news:Entrackr`), date when known, the
  exact headline used as evidence, and a confidence between 0 and 1.
- Agent-Reach's own health check: `agent-reach doctor` (installed as an isolated uv tool).

## 11. Phase 5 qualification gate - step by step

The gate answers one question per company: **QUALIFIED**, **REVIEW** (a human should look) or
**NOT_QUALIFIED**. Only QUALIFIED companies may ever go to Phase 6 enrichment.

**a) Offline demo (safe, nothing written):**
```powershell
uv run python scripts/run_qualification.py
```
**b) Your real companies, read-only, mock Laya:**
```powershell
uv run python scripts/run_qualification.py --db --limit 5
```
**c) Start the local Laya sidecar** (installed once as an isolated tool - it never enters the
app's Python environment). First start downloads the ~800 MB checkpoint to
`%USERPROFILE%\.cache\huggingface`. Keep this window open:
```powershell
uv tool install "laya[serve]==0.3.22"        # one time only
$env:LAYA_HOST="127.0.0.1"; $env:LAYA_PORT="8765"; $env:LAYA_DEVICE="cpu"
$env:LAYA_MODELS="typed-decisions"; $env:LAYA_PRELOAD="1"
laya-serve
```
Check it in a second window: `curl.exe http://127.0.0.1:8765/health` → `"status":"ok"`.
On CPU one company takes about 10–15 seconds.

**d) Real companies with live Laya, and save the decisions:**
```powershell
$env:PROVIDER_MODE="live"; $env:LAYA_ENDPOINT_URL="http://127.0.0.1:8765"
uv run python scripts/run_qualification.py --db --live --persist --limit 5
Remove-Item Env:PROVIDER_MODE; Remove-Item Env:LAYA_ENDPOINT_URL
```
Each run adds NEW rows (`qualification_runs`, `qualification_decisions`); old decisions are
never changed (the database refuses updates).

**e) Evaluation on the labeled (synthetic) cases:**
```powershell
uv run python scripts/evaluate_qualification.py                      # mock Laya
uv run python scripts/evaluate_qualification.py --live --out evaluation\qualification\reports\my_run.json
```
These numbers describe 28 hand-written cases, not real-world accuracy.

**f) Change the ICP** (country, ₹5 Cr cap, 90-day windows, thresholds) in `.env` - see
`docs/phases/QUALIFICATION_POLICY.md` for every setting. Bump nothing in code.

## 12. Phase 6 enrichment - step by step

Enrichment finds a verified work email for the founder/CEO of a **QUALIFIED** company only.
REVIEW and NOT_QUALIFIED companies are refused before any paid call.

**a) Offline demo (safe, nothing written):**
```powershell
uv run python scripts/run_enrichment.py
```
**b) Your real QUALIFIED companies, read-only, mock providers:**
```powershell
uv run python scripts/run_enrichment.py --db --limit 5
```
(Today this prints "no QUALIFIED company" - Drivn is REVIEW.)

**c) One-time sidecar setup** (Node.js 22+; builds the pinned enrichment-kit):
```powershell
cd sidecars\enrichment-kit
node setup.mjs
npm test
cd ..\..
```
**d) Add to `.env`** (keys you already have stay as they are):
```
ENRICHMENT_KIT_BASE_URL=http://127.0.0.1:8787
ENRICHMENT_KIT_API_TOKEN=<any long random string>
```
**e) Start the sidecar** (keep this window open):
```powershell
cd sidecars\enrichment-kit
node server.mjs
```
Check: `curl.exe http://127.0.0.1:8787/health` → `"status":"ok"`, both providers `configured`.

**f) Live run - smallest possible** (costs Hunter/Apollo credits; budgets stop it safely):
```powershell
$env:PROVIDER_MODE="live"; $env:ALLOW_PAID_PROVIDER_CALLS="true"
uv run python scripts/run_enrichment.py --db --live --limit 1
Remove-Item Env:PROVIDER_MODE; Remove-Item Env:ALLOW_PAID_PROVIDER_CALLS
```
The output shows Hunter's own usage counters before/after. Results are stored as new rows
(`enrichment_runs`, `enrichment_contacts`, `enrichment_attempts`) - history is never changed;
`enrichment_cache` makes a repeat run free.

**g) Budgets and thresholds** live in `.env` (`ENRICHMENT_MAX_*`, `ENRICHMENT_*_CONFIDENCE`,
`ENRICHMENT_*_TTL_DAYS`) - see `docs/phases/ENRICHMENT_POLICY.md`.

**h) Source Actor Registry:** discovery refuses any source that is not declared, approved,
login-free, parser-backed and enabled (`DISCOVERY_SOURCES_ENABLED`, default `funding_news`).

## 13. Phase 7 AI lead analysis - step by step

The analysis explains **why** a lead matters and which TechnoTribes offer fits, using only the
collected evidence. It runs only for QUALIFIED companies with a VERIFIED contact.

**a) Offline demo (safe, nothing written, no AI service):**
```powershell
uv run python scripts/run_lead_analysis.py
```
**b) Your real eligible leads, read-only, mock AI:**
```powershell
uv run python scripts/run_lead_analysis.py --db --limit 5
```
(Today this prints "no eligible lead" - no company is QUALIFIED yet.)

**c) Check the AI provider configuration (no call, no cost):**
```powershell
uv run python scripts/check_openai.py
```
**d) OpenAI key:** follow `docs/OPENAI_PROVIDER_NOTES.md` - put `OPENAI_API_KEY=` in `.env`.
Never paste the key anywhere else.

**e) Smallest live test** (one tiny call + a synthetic company, nothing stored):
```powershell
$env:PROVIDER_MODE="live"; $env:ALLOW_PAID_PROVIDER_CALLS="true"
uv run python scripts/check_openai.py --chat --analysis-sample
Remove-Item Env:PROVIDER_MODE; Remove-Item Env:ALLOW_PAID_PROVIDER_CALLS
```
**f) Live analysis of one real eligible lead** (stored, immutable):
```powershell
$env:PROVIDER_MODE="live"; $env:ALLOW_PAID_PROVIDER_CALLS="true"
uv run python scripts/run_lead_analysis.py --db --live --limit 1
Remove-Item Env:PROVIDER_MODE; Remove-Item Env:ALLOW_PAID_PROVIDER_CALLS
```
**g) Rules, budgets, model policy:** `docs/phases/LEAD_ANALYSIS_POLICY.md`.

## 14. Phase 8 outputs - Google Sheet, dashboard, human review

The database is the source of truth. The Sheet and the dashboard only show it.

**a) Sheet export - offline preview (safe, nothing written):**
```powershell
uv run python scripts/export_leads_to_sheets.py
```
**b) Your real leads, preview only:**
```powershell
uv run python scripts/export_leads_to_sheets.py --db --limit 5
```
**c) Check the real Sheet without writing (credentials, workbook, current rows):**
```powershell
uv run python scripts/export_leads_to_sheets.py --db --check
```
**d) Write to the real Sheet** (only QUALIFIED companies reach the Leads tab):
```powershell
$env:ALLOW_SHEETS_WRITES="true"
uv run python scripts/export_leads_to_sheets.py --db --live --limit 5
Remove-Item Env:ALLOW_SHEETS_WRITES
```
Running it again only updates rows that changed - it never adds duplicate rows. Every run is
recorded in `export_runs` / `export_items` and as one row in the `Export_Runs` tab.

**e) Dashboard:**
```powershell
uv run python scripts/run_dashboard.py
```
Open http://127.0.0.1:8000/dashboard (Ctrl+C stops it). `--demo` shows synthetic data without
the database. Click a company in **Review queue**, type your name and a reason, choose
approve / reject / reopen, **submit**. Approve only works when the hard ICP rules pass; every
action is kept in the history. Approved companies are enriched/analysed by the normal Phase 6/7
commands - the dashboard never calls a paid service.

**f) Sheet access problems:** `docs/GOOGLE_SHEETS_SETUP_GUIDE.md`.
**g) Rules and the full column list:** `docs/phases/OUTPUTS_POLICY.md`.

## 15. Phase 9/10 - pipeline, OpenAI, operations

```powershell
uv run python scripts/run_pipeline.py                 # pipeline dry run, offline demo
uv run python scripts/run_pipeline.py --db            # pipeline dry run, real data
uv run python scripts/run_pipeline.py --db --status   # recent runs
uv run python scripts/check_openai.py                 # AI provider check (free)
uv run python scripts/backup_db.py                    # database backup
uv run python scripts/restore_db.py --file backups\<file>   # restore test (separate database)
```

Live runs, the dashboard token, recovery and the demo are described step by step in
`docs/OPERATOR_GUIDE.md` and `docs/DEMO_RUNBOOK.md`. Tests that need PostgreSQL use the
separate `lead_intel_test` database on the same server.

## 16. Where things are

| Need | File |
|---|---|
| Current state of the project | `docs/PROJECT_STATE.md` |
| What changed and when | `docs/CHANGELOG.md` |
| Why decisions were made | `docs/decisions/ARCHITECTURE_DECISIONS.md` |
| Provider keys/setup | `docs/providers/PROVIDER_SETUP.md` |
| **How to operate the system** | `docs/OPERATOR_GUIDE.md` |
| Demo for a manager | `docs/DEMO_RUNBOOK.md` |
| Install / upgrade / network check | `docs/DEPLOYMENT_RUNBOOK.md` |
| Backups | `docs/BACKUP_AND_RESTORE.md` |
| Security review, key rotation | `docs/SECURITY_REVIEW.md` |
| OpenAI setup | `docs/OPENAI_PROVIDER_NOTES.md` |
| Phase reports | `docs/phases/PHASE_1..10_COMPLETION.md` |
| Sheet columns, review rules, dashboard | `docs/phases/OUTPUTS_POLICY.md` |
| Google Sheet access | `docs/GOOGLE_SHEETS_SETUP_GUIDE.md` |
| Lead analysis rules / offer catalog | `docs/phases/LEAD_ANALYSIS_POLICY.md`, `app/knowledge/service_catalog.json` |
| OpenAI key setup | `docs/OPENAI_PROVIDER_NOTES.md` |
| Enrichment rules & settings | `docs/phases/ENRICHMENT_POLICY.md` |
| Enrichment sidecar | `sidecars/enrichment-kit/README.md` |
| Qualification rules & settings | `docs/phases/QUALIFICATION_POLICY.md` |
| Labeled evaluation set | `evaluation/qualification/` + `docs/evaluation/QUALIFICATION_EVALUATION_SET.md` |
| Secrets (never share/commit) | `.env` |
