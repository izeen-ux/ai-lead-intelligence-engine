# Operator Guide — Lead Intelligence Engine

The one guide you need to start, run, watch, stop and recover the system. Every command is
typed in **PowerShell**, in the project folder (`E:\MY PROJECT'S\AI_Lead_Intelligence`).

```text
START SERVICES → START APP → CHECK /health + /health/ready → TRIGGER PIPELINE
→ WATCH RUN IN DASHBOARD → VIEW LEADS / REVIEW QUEUE → OPEN GOOGLE SHEETS OUTPUT
→ INSPECT LEAD ANALYSIS
```

## 0. Three safety switches (never change them permanently)

Your `.env` file keeps the safe baseline:

```
PROVIDER_MODE=mock                # no real provider is called
ALLOW_PAID_PROVIDER_CALLS=false   # nothing can spend money
ALLOW_SHEETS_WRITES=false         # the Google Sheet is not written
```

To spend money or write the Sheet you set a switch **for one command only** (shown below) and
remove it right after. If you close the PowerShell window, the switches are gone anyway.

Secrets live only in `.env` and `secrets/google/`. Never paste them into chat, email or a
screenshot.

## 1. START SERVICES + START APP (one command)

Docker Desktop must be running (start it from the Start menu, wait for "Engine running").

```powershell
docker compose up -d
```

This starts everything: database, Crawl4AI, Laya, enrichment sidecar, the app (API +
dashboard) and the scheduler. Check that all are up:

```powershell
docker compose ps
```

You should see `app`, `postgres`, `crawl4ai`, `laya`, `enrichment-kit` **healthy**,
`scheduler` running and `migrate` exited (0) — `migrate` only updates the database tables and
then stops. Laya needs 1–3 minutes on first start.

After you change code or `.env`: `docker compose up -d --build`.

## 2. CHECK /health + /health/ready

```powershell
curl.exe http://127.0.0.1:8000/health
curl.exe http://127.0.0.1:8000/health/ready
```

- `/health` → `"status":"ok"` = the app is alive.
- `/health/ready` → `"status":"ok"` = database, tables and sidecar are ready. `degraded` =
  it works but something is missing (read the `checks` list). `error` = fix before using.

Neither check calls a paid provider.

Check the AI provider (one free call, no generation):

```powershell
uv run python scripts/check_openai.py
```

## 3. Open the dashboard

Open **http://127.0.0.1:8000/dashboard**

The first time, the page asks for the **operator token**: open `.env`, copy the value after
`OPERATOR_API_TOKEN=`, paste it into the "operator token" box (top right) and click
**use token**. The browser forgets it when you close the tab.

## 4. TRIGGER PIPELINE

A pipeline run executes the stages in order: website → signals → qualification → enrichment
→ analysis → export. **Discovery** (finding new companies) spends Apify credit, so it only
runs when you ask for it with `--stages all` (or add `discovery` to `PIPELINE_STAGES`). Only
one run can be active.

`--limit N` = how many companies each stage may process in this run. Without it the run uses
`PIPELINE_COMPANY_LIMIT` (5). Any value up to `PIPELINE_MAX_COMPANY_LIMIT` (25) is honoured
exactly; a bigger value is refused with a message - it is never silently reduced.

**a) Dry run (safe, writes nothing):** in the dashboard, section **Pipeline runs**: type your
name → **start dry run**. Or:

```powershell
uv run python scripts/run_pipeline.py --db
```

**b) Tiny live run** (really calls providers; spends within the budgets). It runs as a
one-off container next to the stack, so it can reach the private Laya and enrichment services.
The live switches apply to this single command only:

```powershell
docker compose run --rm -e PROVIDER_MODE=live -e ALLOW_PAID_PROVIDER_CALLS=true app python scripts/run_pipeline.py --db --live --limit 1
```

Run only some stages, for example without the website stage (Firecrawl is the stage that
costs credits for new companies):

```powershell
docker compose run --rm -e PROVIDER_MODE=live -e ALLOW_PAID_PROVIDER_CALLS=true app python scripts/run_pipeline.py --db --live --stages signals,qualification,enrichment,analysis,export --limit 1
```

Add `-e ALLOW_SHEETS_WRITES=true` if the export stage should really write the Google Sheet.
Without a qualified company the run simply reports every stage as "skipped" and costs nothing.

**Find and process several NEW companies in one run** (discovery included). This is the
command to use when the lead list looks empty:

```powershell
docker compose run --rm -e PROVIDER_MODE=live -e ALLOW_PAID_PROVIDER_CALLS=true -e APIFY_MAX_ITEMS_PER_RUN=4 -e DISCOVERY_SOURCES_ENABLED=funding_news,startup_news app python scripts/run_pipeline.py --db --live --stages all --limit 10
```

| Part | Meaning | Cost guide |
|---|---|---|
| `--stages all` | include discovery | - |
| `APIFY_MAX_ITEMS_PER_RUN=4` | 4 Google searches per source in this run (default 1) | $0.001 + $0.0045 per search, per source |
| `DISCOVERY_SOURCES_ENABLED=funding_news,startup_news` | funding headlines **and** launch / expansion headlines (default: funding only) | doubles the searches |
| `--limit 10` | up to 10 companies per stage | Firecrawl ≤ `FIRECRAWL_MAX_CREDITS_PER_RUN`, Hunter ≤ 5 credits, OpenAI ≤ $0.25 |

Each run starts one search further into the query list (`DISCOVERY_QUERIES`,
`DISCOVERY_NEWS_QUERIES` in `.env`), so repeating the command finds different companies.
Expect far fewer qualified leads than discovered companies: a company must be Indian, have a
verified website, and either a round of at most ₹5 Cr or a recent intent signal. A run that
ends with 0 new qualified leads is a correct result, not a failure.

**c) Automatic runs:** set `PIPELINE_SCHEDULE_INTERVAL_MINUTES=1440` in `.env` (once a day; 0 =
off) and run `docker compose up -d`. The scheduler makes **dry runs** unless the stack was
started in live mode (§9).

## 5. WATCH RUN IN DASHBOARD

Section **Pipeline runs** lists every run: who started it, dry run or LIVE, status, current
stage, estimated cost, error. Click a row to see each stage (how many companies, why a stage
was skipped, the error if one failed). From PowerShell:

```powershell
uv run python scripts/run_pipeline.py --db --status
```

| Status | Meaning |
|---|---|
| running | in progress |
| succeeded | all requested stages finished |
| failed | a stage failed; later stages were **not** run (see the error) |
| cancelled | the app or command was stopped while it ran |
| abandoned | the process died; closed by recovery (§8) |

## 6. VIEW LEADS / REVIEW QUEUE

- **Leads** table: choose *qualified / review / not qualified / all companies*. Click a company.
- **Review queue**: companies that need a human decision. Click one, read the hard rules,
  reasons and evidence, type your name and a reason, choose *approve / reject / reopen*,
  **submit**. Approve only works when the hard ICP rules pass. Every action is kept in the
  history; nothing is overwritten.

## 7. INSPECT LEAD ANALYSIS + OPEN GOOGLE SHEETS OUTPUT

Click a qualified lead. The detail panel shows:
- **Trace** – the ids of the company, decision, contact and analysis;
- **Qualification** – each hard rule and the reasons;
- **Evidence** – the news events with their source links;
- **Contact** – founder name, title, verified email, phone + phone status, LinkedIn URL;
- **Company profile & field provenance** – location, industry, company LinkedIn, funding, and
  for every field where the value came from (`NOT_FOUND` when the evidence has none);
- **AI analysis / offer angle** – facts, hypotheses, opportunity, recommended offer, why, the
  outreach hook, confidence and which model produced it;
- **Export** – whether the row is in the Google Sheet.

Google Sheet (tabs `Leads`, `Review_Queue`, `Export_Runs`):

```powershell
uv run python scripts/export_leads_to_sheets.py --db --check      # read only: what would change
$env:ALLOW_SHEETS_WRITES="true"
uv run python scripts/export_leads_to_sheets.py --db --live       # write
Remove-Item Env:ALLOW_SHEETS_WRITES
```

Then open your Google Sheet in the browser. Edits made in the Sheet are **not** saved back.

The `Leads` tab starts with: lead id, company, website, location, industry, founder name /
title / email / email status / phone / phone status / LinkedIn, company LinkedIn, funding
amount / round / date, qualification decision / path / confidence, trigger, verified contact,
AI opportunity, recommended offer, reason, outreach hook, signal date, evidence links, record
ids, last update, export status (full list: `docs/phases/OUTPUTS_POLICY.md` §2).

**Blank cells are normal.** Phone, LinkedIn, industry and funding are filled only when a
provider or the company's own website really supplied them. `phone_status = not_requested`
means the phone option is off (the default). To switch it on for a run add
`-e ENRICHMENT_PHONE_ENABLED=true`: it keeps a number Hunter already returns with the email
(no extra credit); many contacts still have none. Nothing is ever guessed.

A `Leads` tab created before 2026-10-05 has the old 26 columns; the first live export converts
it in place (all rows are moved to the new columns, none is lost).

## 8. STOP / RESTART / RECOVER

```powershell
docker compose down            # stop everything (your data stays in the database volume)
docker compose up -d           # start again
docker compose restart app     # restart only the app
docker compose logs -f app scheduler      # watch the logs (Ctrl+C to leave)
```

If a run stays "running" although nothing is happening (the computer was switched off):

```powershell
uv run python scripts/run_pipeline.py --db --recover
```

or click **recover stale run** in the dashboard. It only closes runs without a sign of life
for `PIPELINE_STALE_AFTER_MINUTES` (60); the scheduler also does this every minute.

## 9. Running the whole stack in live mode (only when you mean it)

```powershell
$env:PROVIDER_MODE="live"; $env:ALLOW_PAID_PROVIDER_CALLS="true"
docker compose up -d app scheduler
Remove-Item Env:PROVIDER_MODE; Remove-Item Env:ALLOW_PAID_PROVIDER_CALLS
```

Back to safe mode: `docker compose up -d app scheduler` (without the `$env:` lines).
The dashboard shows the mode next to "Pipeline runs".

## 10. Check provider health without spending money

Dashboard → **Provider health** (configuration only, no call is made), or:

```powershell
curl.exe http://127.0.0.1:8000/health/ready
uv run python scripts/check_openai.py
```

Spend so far: dashboard → **Usage & cost**. Budgets: `OPENAI_MAX_ESTIMATED_COST_USD_PER_RUN`,
`OPENAI_MAX_ESTIMATED_COST_USD_PER_MONTH`, `PIPELINE_MAX_ESTIMATED_COST_USD_PER_RUN` and the
`ENRICHMENT_MAX_*` settings in `.env`.

## 11. Back up and restore the database

```powershell
uv run python scripts/backup_db.py
uv run python scripts/restore_db.py --file backups\<the file it printed>
```

The second command restores into a separate scratch database and compares it with the real
one — it never touches your data. Details: [BACKUP_AND_RESTORE.md](BACKUP_AND_RESTORE.md).

## 12. Who may do what

| Action | Who |
|---|---|
| Look at `/health`, `/health/ready` | anyone on this computer |
| Dashboard, leads, analyses, usage | whoever has the operator token |
| Start a pipeline run, recover a run, review a lead | whoever has the operator token (the name you type is recorded) |
| Live runs, Sheet writes, backups, restores, `.env` | the person with access to this computer and `.env` |

## 13. If something goes wrong

| What you see | What to do |
|---|---|
| Dashboard: "operator token required" | §3 |
| `docker compose ps` shows a service "restarting" | `docker compose logs <service> --tail 20` |
| `provider_not_configured … OpenAI` | the key in `.env` is missing/revoked → [OPENAI_PROVIDER_NOTES.md](OPENAI_PROVIDER_NOTES.md) |
| `provider_budget_exceeded` / `budget_exhausted` | the budget did its job; raise it in `.env` only if intended |
| "a pipeline run is already in progress" | wait, or §8 recover if it is stale |
| Sheet errors | [GOOGLE_SHEETS_SETUP_GUIDE.md](GOOGLE_SHEETS_SETUP_GUIDE.md) |
| Anything else | `docker compose logs app --tail 50` and look for `"level": "ERROR"` |

Demo for a manager: [DEMO_RUNBOOK.md](DEMO_RUNBOOK.md). Installing on another machine:
[DEPLOYMENT_RUNBOOK.md](DEPLOYMENT_RUNBOOK.md).
