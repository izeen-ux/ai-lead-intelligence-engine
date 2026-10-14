# Security Review — release candidate (2026-10-05)

Scope: the application, its Compose deployment on a single machine, and the provider
boundaries. Method: code review of every boundary, automated tests (`tests/unit/
test_operator_api_security.py`, `test_phase10_hardening.py`, `test_openai_adapter.py`,
`test_repo_hygiene.py` and the existing provider/guard tests), a dependency scan and checks
against the running stack. Residual risks are listed honestly in §3.

## 1. Findings by area

| Area | State | Evidence |
|---|---|---|
| **Secret handling** | Secrets only in git-ignored `.env` / `secrets/`; `SecretStr` in settings; never in images (`.dockerignore`), never in `.env.example` (test), never printed by any CLI (key presence shown as yes/NO). Operator and sidecar tokens were generated locally without being displayed | `test_repo_hygiene.py`; scan of all 277 non-ignored files for 12 real secret values: 0 hits; Dockerfile test |
| **Logging redaction** | JSON logs with request id, pipeline run id, stage, provider, status, error code, duration, token total, estimated cost. Redaction filter masks secret-named keys, known secret values in messages, **nested** extras and **tracebacks** (added in Phase 10). Prompts, model output, emails and full payloads are not logged | `test_log_lines_carry_run_context_and_redact_everywhere`; OpenAI log test; 27,962 characters of live container logs scanned for the secret values: 0 hits |
| **Network exposure** | Host listeners: `127.0.0.1:8000`, `127.0.0.1:5432` + `[::1]:5432`, `127.0.0.1:11235`. Laya and enrichment-kit publish no port (private Compose network). The previous `0.0.0.0:5432` binding is gone | `netstat` on the running stack; Compose test; host → 8765/8787 connection refused |
| **API authentication** | Every `/api/v1` route requires the operator token when configured (constant-time compare); the app refuses to start in staging/production without a 24+ character token; without a token only loopback clients are served. `/health` is minimal; `/health/ready` contains configuration states only | 36 tests in `test_operator_api_security.py`; live: 401 without token, 200 with |
| **Write actions** | Exactly three write endpoints (review, pipeline trigger, pipeline recover). All need token + `X-Dashboard-Request` header (blocks cross-site form posts). A writing pipeline run is refused unless the process is in live mode; duplicate runs are refused | OpenAPI test lists the write paths; 409 tests |
| **CORS** | No CORS headers by default (same-origin only). `CORS_ALLOWED_ORIGINS` accepts exact origins; wildcards are a startup error; credentials are never allowed | CORS tests |
| **Request limits** | Body limit 64 KB (`MAX_REQUEST_BODY_BYTES`) → 413; list endpoints paginate with `limit ≤ 100`; provider calls all have timeouts and bounded retries; AI output bounded by `OPENAI_MAX_TOKENS`; evidence bundle and page excerpts are length-capped | body-limit test; adapter tests |
| **Browser hardening** | CSP (`default-src 'self'`, no framing, no form posts), `X-Frame-Options: DENY`, `nosniff`, `no-referrer`, API responses `no-store`. The dashboard writes text only (`textContent`), links only `http(s)`; the token lives in `sessionStorage` | header test; page test (`innerHTML` absent) |
| **SQL boundary** | All queries use SQLAlchemy Core/ORM or `text()` with bound parameters; API parameters are typed (UUID / enum / bounded int) and rejected by validation otherwise; free text is stored as data. Backup/restore validates identifiers against a strict pattern before use | injection tests (`' OR '1'='1`, `;DROP TABLE`, SQL in free text) |
| **File / path handling** | The app serves one fixed file (`dashboard.html`); no static directory, no user-supplied path. Backup files are written to a fixed folder with a generated name; restore reads a path given by the local operator | traversal tests (`/dashboard/../.env`, encoded variants, `/secrets/...`) |
| **SSRF / outbound** | Crawl targets must be public DNS names: reserved suffixes, social/non-official hosts, **IP literals, numeric hosts and bare internal names are refused** (added in Phase 10). Sidecar URLs must be loopback or an explicitly allow-listed private service name. Router/OpenAI base URLs come from operator configuration only | `test_crawl_targets_can_never_be_internal`; sidecar rule tests |
| **Container permissions** | App/scheduler: non-root uid 10001, read-only root filesystem, `/tmp` tmpfs, all capabilities dropped, `no-new-privileges`. Sidecars non-root. No dev dependencies in the image | `id` and `touch` inside the running container; Dockerfile test |
| **Paid-provider safety** | Unchanged baseline: `PROVIDER_MODE=mock`, `ALLOW_PAID_PROVIDER_CALLS=false`, `ALLOW_SHEETS_WRITES=false`. Paid guard runs before credentials and before any request for every paid adapter incl. OpenAI. Budgets: per call (worst case), per run, per month, per pipeline | guard tests for all providers; OpenAI budget tests |
| **OpenAI response handling** | Output is untrusted: strict schema, then domain validation (evidence ids must exist, offer must be in the catalog, no contact data in the hook). Refusals, incomplete and malformed answers are typed errors and never stored as recommendations. No tools/web search are enabled; `store=False` | adapter + analysis tests; live run |
| **External content trust** | Crawled pages and news text are data, never instructions to the system: they are excerpted, length-capped and passed inside a JSON user message; the model's answer cannot trigger actions (no tools) and must cite ids from the bundle. A page that tries to inject instructions can at worst produce an analysis that fails validation or a misleading *hypothesis* — it cannot change decisions, contacts or exports | analysis policy; grounding tests |
| **Gates** | Qualification, enrichment and analysis gates are re-checked inside each service; the pipeline cannot skip them; human approval re-evaluates the hard rules; the pipeline never re-decides a human decision | pipeline + review tests |
| **Dependencies** | `pip-audit` on the 56 locked runtime packages: no known vulnerabilities. `npm audit --omit=dev` on the enrichment-kit build: 0 vulnerabilities. Base images: `python:3.13-slim-bookworm`, `node:22-bookworm-slim`, `postgres:16`, `unclecode/crawl4ai:0.9.4` (pinned tag) | scan output 2026-10-05 |
| **Data protection** | Backups verified by restore; append-only triggers protect history; the Sheet only receives verified work emails of qualified leads; demo data can only reach `FIXTURE_` tabs | backup report; export tests |

## 2. Changes made by this review

1. PostgreSQL no longer published on `0.0.0.0`.
2. Operator token on all API routes; mandatory in staging/production.
3. Explicit CORS allow-list, body-size limit, security headers.
4. Redaction of nested log fields and tracebacks; a log field named `total_tokens` was being
   masked by the redaction rule and was renamed.
5. Crawl targets: IP literals and internal names refused.
6. Sidecar endpoints: loopback or allow-listed private names only; the enrichment sidecar
   refuses a non-loopback bind without its bearer token.
7. Demo/fixture data can never be written to the official Sheet tabs.

## 3. Residual risks (accepted for this release)

| # | Risk | Why accepted / mitigation |
|---|---|---|
| 1 | **One shared operator token**, no per-user accounts; the reviewer name is typed by the user | Internal single-team tool. Use a reverse proxy with SSO for more users; rotate the token when someone leaves |
| 2 | **No TLS in the app** | It listens on loopback only. Remote access must go through an HTTPS reverse proxy (Deployment Runbook §6) |
| 3 | **Secrets in a plaintext `.env`** on the laptop | Git-ignored; use disk encryption (BitLocker). A secret manager is the next step for a real server |
| 4 | **Containers have unrestricted outbound internet** | Needed for the providers. Crawl target validation is name-based (no DNS-resolution check), so a public DNS name that resolves to a private address is not detected; crawling runs inside Firecrawl (hosted) or the Crawl4AI container, not the app |
| 5 | **Upstream enrichment-kit sends the Hunter key in the URL query** (its own code, pinned commit) | Traffic is HTTPS; revisit on upgrade |
| 6 | **Backups are not encrypted by the script** | Store on an encrypted disk; documented |
| 7 | **AI cost is an estimate** (tokens × configured price) | Budgets are conservative; set a hard monthly limit in the OpenAI project too |
| 8 | **Prompt injection through crawled text** can bias an analysis' wording | No tools, strict schema, grounding validation, human reads the lead before outreach; outreach is not automated |
| 9 | **A review action is two short transactions** (decision, then audit row) | The decision itself records reviewer and reason |
| 10 | `python:3.13-slim` / `node:22-slim` / Crawl4AI images are pinned by tag, not digest | Rebuild regularly; pin digests for a production server |
| 11 | Rate limiting of the API is not implemented | Loopback + token; add at the reverse proxy if exposed |
| 12 | The old PostgreSQL container (`lead-intel-pg-old-0.0.0.0`) still exists, stopped | Delete it once the new setup is trusted (Deployment Runbook §8) |

## 4. Credential rotation

| Secret | How to rotate |
|---|---|
| `OPENAI_API_KEY`, `APIFY_TOKEN`, `FIRECRAWL_API_KEY`, `HUNTER_API_KEY`, `APOLLO_API_KEY` | create a new key in the provider dashboard → replace in `.env` → `docker compose up -d` → verify (`/health/ready`, `check_openai.py`) → revoke the old key |
| `OPERATOR_API_TOKEN`, `ENRICHMENT_KIT_API_TOKEN`, `CRAWL4AI_API_TOKEN` | replace with a new random value in `.env` → `docker compose up -d` |
| Google service-account key | create a new key for the service account → replace `secrets/google/service-account.json` → `docker compose restart app scheduler` → delete the old key in Google Cloud |
| Database password | `ALTER USER postgres PASSWORD '…'` inside the container → update `DATABASE_URL` → `docker compose up -d` |

Rotate at once if a secret was pasted into chat/email, committed, or a device is lost.
