# Provider Setup & Credential Contract

**Phase 6 status:** Apify, Firecrawl, Crawl4AI, Agent-Reach (public channels only), Laya (local
sidecar) and enrichment-kit + Hunter + Apollo (local enrichment sidecar) are live-capable. The AI
Router reviewer remains a non-operational contract. Default remains `PROVIDER_MODE=mock`, `ALLOW_PAID_PROVIDER_CALLS=false`. The Apify token was
rotated by the user on 2026-09-29 (presence/format verified; value never shown).

> Account portals, free-tier limits and pricing change. Everything marked **verify** is checked
> against the provider's official documentation at the start of the phase that integrates it,
> and this file is updated then. Nothing here is a claim about current pricing.

## General rules (all providers)

1. Put keys only in `.env` (git-ignored) or a secret manager. Never in code, docs, tickets or chat.
2. `.env.example` keeps every key **empty**; a test fails if a secret-like key gets a value.
3. To make a real call you must set **both** `PROVIDER_MODE=live` and, for paid providers,
   `ALLOW_PAID_PROVIDER_CALLS=true`. Turn the second one off again after the run.
4. Check wiring without spending anything: `GET /health/ready` → `providers[].configured`.
5. Use a separate key per environment (dev / staging / production) so one can be revoked alone.
6. Rotation: create new key → update `.env`/secret store → restart app → confirm
   `/health/ready` → revoke old key in the provider dashboard.
7. On suspected leak: revoke immediately in the dashboard, rotate, check provider usage logs.

## Summary

| Provider | Phase | Env vars | Paid guard | Needs an account? |
|---|---|---|---|---|
| Apify | 2 ✅ live-capable | `APIFY_TOKEN` + run ceilings (see below) | yes | yes |
| Firecrawl | 3 ✅ live-capable | `FIRECRAWL_API_KEY` + budgets (see below) | yes | yes (hosted API) |
| Crawl4AI | 3 ✅ live-capable | `CRAWL4AI_BASE_URL`, `CRAWL4AI_API_TOKEN` (local container) | no | no (open source, Apache-2.0) |
| Agent-Reach | 4 ✅ live-capable (public) | `AGENT_REACH_CHANNELS` + budgets (no secrets) | no | no (uv tool) |
| Laya | 5 ✅ live-capable (local sidecar) | `LAYA_ENDPOINT_URL` (loopback only), optional `LAYA_API_KEY` | no | no (open source, Apache-2.0; uv tool) |
| enrichment-kit | 6 ✅ live-capable (local sidecar) | `ENRICHMENT_KIT_BASE_URL`, `ENRICHMENT_KIT_API_TOKEN` | yes (spends Hunter/Apollo credits) | no (open source, MIT) |
| Hunter | 6 ✅ via sidecar | `HUNTER_API_KEY` (read by the sidecar) | yes | yes |
| Apollo | 6 ✅ via sidecar | `APOLLO_API_KEY` (read by the sidecar) | yes | yes |
| Google Sheets (output) | 8 ✅ live-capable (service account) | `GOOGLE_SERVICE_ACCOUNT_JSON` (key file path), `GOOGLE_SHEETS_SPREADSHEET_ID`, `ALLOW_SHEETS_WRITES` | own switch | Google Cloud project + Sheet shared with the service account |
| **OpenAI** (reasoning) | 10 ✅ live-verified | `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_MAX_*` budgets | yes | yes (OpenAI API project) |
| Techno-AI-Router | **deprecated** (opt-in: `REASONING_PROVIDER=ai_router`) | `AI_ROUTER_*` — not required | yes | internal TechnoTribes service |
| PostgreSQL | 2 ✅ in use | `DATABASE_URL` (+ optional `TEST_DATABASE_URL`) | n/a | local Docker (`lead-intel-pg`) |

---

## Apify — discovery (Phase 2) — **LIVE-CAPABLE**
- **Role:** run the discovery Actor; results go to normalization/dedupe/PostgreSQL, never to
  enrichment.
- **Status:** integrated (official `apify-client` 3.2.x). Token configured locally in `.env`.
- **Obtain:** Apify Console → Settings → API & Integrations → personal API token.
- **Configure (`.env`, never `.env.example`):**
  | Variable | Default | Meaning |
  |---|---|---|
  | `APIFY_TOKEN` | — (secret) | personal API token |
  | `APIFY_DISCOVERY_ACTOR_ID` | `apify/google-search-scraper` | Actor to run (must have a registered spec) |
  | `APIFY_MAX_ITEMS_PER_RUN` | `1` | hard ceiling on dataset items (1 item = 1 Google results page ≈ 10 results) |
  | `APIFY_MAX_CHARGE_USD_PER_RUN` | `0.50` | Apify-enforced hard cap (`max_total_charge_usd`); **Apify minimum is $0.50** |
  | `APIFY_MAX_ESTIMATED_COST_USD_PER_RUN` | `0.05` | tight budget: run blocked *before* calling Apify if the worst-case estimate exceeds this |
  | `APIFY_RUN_TIMEOUT_SECONDS` | `180` | Actor run timeout |
- **Actor & cost (verified via Apify API 2026-09-28, FREE tier):** `apify/google-search-scraper`,
  pay-per-event: `actor-start` $0.001, `search-page-scraped` $0.0045 → **≈ $0.0055 per 1-page
  run**. Higher Apify plans are cheaper per page. Paid add-ons (leads enrichment, AI engines,
  website content, ads) are explicitly disabled in the Actor input.
- **Sources (Source Actor Registry, `DISCOVERY_SOURCES_ENABLED`):** both use this one public
  Google Search Actor with the same pinned-safe input - no login, no cookies.
  | Source | Reads | Default |
  |---|---|---|
  | `funding_news` | funding headlines ("X raises Rs 3 Cr seed round") | enabled |
  | `startup_news` | launch / expansion / hiring headlines ("X expands to 5 cities") - feeds the intent path | approved, **off** until listed |
  LinkedIn, X/Twitter (blocked) and Reddit, YouTube, job boards, startup databases,
  directories (pending review) cannot be enabled by configuration.
- **Query sets:** `DISCOVERY_QUERIES` (funding) and `DISCOVERY_NEWS_QUERIES` (news),
  `;`-separated; `DISCOVERY_QUICK_DATE_RANGE=m3` limits results to the 90-day trigger window.
  The pipeline's discovery stage sends `min(APIFY_MAX_ITEMS_PER_RUN, queries)` queries per
  source (one results page each) and rotates through the set from run to run.
  **Cost per source = $0.001 + $0.0045 x queries** (1 query ≈ $0.0055, 4 ≈ $0.019, 10 ≈
  $0.046); the run is blocked before calling Apify when that exceeds
  `APIFY_MAX_ESTIMATED_COST_USD_PER_RUN`. Raise `APIFY_MAX_ITEMS_PER_RUN` (1..20) to discover
  more companies per run; the default 1 stays conservative.
- **Dry run (free, offline):** `uv run python scripts/run_discovery.py --limit 5`
- **Live run (costs credits):** set `PROVIDER_MODE=live` and `ALLOW_PAID_PROVIDER_CALLS=true`
  (ideally only for the one command), then
  `uv run python scripts/run_discovery.py --live --limit 5 --max-items 1 --max-charge-usd 0.50`.
  Turn `ALLOW_PAID_PROVIDER_CALLS` back to `false` afterwards.
- **Check usage:** Apify Console → Actors → Runs (the run id is stored in
  `discovery_runs.provider_run_id`) and Console → Billing/Usage.
- **Revoke/rotate:** Console → API & Integrations → create a new token, update `.env`, delete the
  old token. **Done 2026-09-29:** rotated by the user after the old token briefly appeared in a
  tracked template / local session diff (never committed or pushed).
- **Compliance:** only public search results are collected; no platform login. Respect the terms
  of the publishers whose pages are later fetched.

## Firecrawl — website intelligence + web search (Phase 3) — **LIVE-CAPABLE**
- **Role:** (1) general web search for official-domain resolution (Brave deferred);
  (2) primary website extraction: map a company's site, scrape ≤ 8 priority pages.
- **Integration:** REST API v2 via `httpx2` (ADR-025) — not the SDK.
- **Obtain:** firecrawl.dev → dashboard → API keys (keys start with `fc-`).
- **Configure (`.env` only):**
  | Variable | Default | Meaning |
  |---|---|---|
  | `FIRECRAWL_API_KEY` | — (secret) | API key (configured locally: yes) |
  | `FIRECRAWL_BASE_URL` | empty → `https://api.firecrawl.dev/v2` | only for self-hosting (AGPL-3.0 review first) |
  | `FIRECRAWL_TIMEOUT_SECONDS` | `60` | per-request timeout |
  | `WEBSITE_MAX_PAGES_PER_COMPANY` | `8` | page budget per company (also caps Crawl4AI) |
  | `FIRECRAWL_MAX_CREDITS_PER_COMPANY` | `15` | worst case search 2 + map 1 + pages |
  | `FIRECRAWL_MAX_CREDITS_PER_RUN` | `50` | run budget; `--max-credits` can only lower it |
  | `DOMAIN_SEARCH_RESULT_LIMIT` | `5` | ≤10 keeps search at 2 credits |
  | `DOMAIN_MIN_CONFIDENCE` | `0.7` | below → REVIEW |
- **Credit costs (docs.firecrawl.dev, verified 2026-09-29):** search 2 / 10 results; map 1 /
  call; scrape 1 / page (billed whenever a document is returned, even 404); JSON format +4 and
  PDF parsing +1 — **never requested**. Free plan: 1,000 credits/month (verify current plan in
  the dashboard). Balance check (free): `GET /v2/team/credit-usage`.
- **Dry run:** `uv run python scripts/run_website_intelligence.py`
- **Live run (costs credits):** `PROVIDER_MODE=live`, `ALLOW_PAID_PROVIDER_CALLS=true` for the one
  command, then e.g. `uv run python scripts/run_website_intelligence.py --live --limit 1 --pages 3 --max-credits 10`.
  The script prints the Firecrawl balance before and after.
- **Revoke/rotate:** dashboard → API keys → create new key, update `.env`, delete the old one.

## Crawl4AI — fallback crawler (Phase 3) — **LIVE-CAPABLE (local, free)**
- **Role:** only when Firecrawl failed or returned insufficient content (e.g. JS-rendered sites),
  or when forced for a smoke test. Same page budget; social URLs never fetched.
- **Runtime:** official Docker image, pinned (ADR-026), bound to localhost only:
  ```bash
  # one-time: generate a token and put CRAWL4AI_API_TOKEN=<token> and
  # CRAWL4AI_BASE_URL=http://127.0.0.1:11235 in .env (never commit), then:
  docker run -d --name lead-intel-crawl4ai --restart unless-stopped \
    -p 127.0.0.1:11235:11235 --shm-size=1g \
    -e CRAWL4AI_API_TOKEN=<same token> unclecode/crawl4ai:0.9.4
  curl http://127.0.0.1:11235/health     # {"status":"ok","version":"0.9.4"}
  ```
  Status now: container `lead-intel-crawl4ai` running; token + base URL configured in `.env`.
- **Configure:** `CRAWL4AI_BASE_URL`, `CRAWL4AI_API_TOKEN` (secret, local only),
  `CRAWL4AI_TIMEOUT_SECONDS` (`90`).
- **Security:** since 0.9 the server is secure-by-default (401 without token, verified). Never
  publish the port beyond 127.0.0.1. No browser cookies/sessions are used or stored.
- **Cost:** local compute only; image ≈ 6.1 GB on disk, uses ~1 GB shared memory while crawling.
- **Fallback-only smoke (free):** `uv run python scripts/run_website_intelligence.py --live --company-id <uuid> --domain <domain> --force-fallback --pages 2`
  (needs `PROVIDER_MODE=live`, not the paid switch).
- **Rotate token:** generate a new one, update `.env`, recreate the container with it.

## Agent-Reach — social/web signal research (Phase 4) — **LIVE-CAPABLE (public, free)**
- **What it is:** Panniantong/Agent-Reach **1.5.0**, pinned commit
  `a19a171fa980a0785849596492e0af4db800c82f` (2026-09-15), MIT. A capability layer that
  installs/health-checks upstream tools; it has **no Python API**. Our `AgentReachAdapter` routes
  to the same upstream backends for the permitted channels (ADR-033).
- **Install (isolated, done):** a `uv` tool outside the project venv; nothing system-wide:
  ```powershell
  uv tool install --with-executables-from yt-dlp "https://github.com/Panniantong/agent-reach/archive/a19a171fa980a0785849596492e0af4db800c82f.zip"
  agent-reach doctor          # read-only channel check (output is in Chinese)
  ```
  Do **not** run `agent-reach install --system` or any `configure ...-cookies` command.
- **Doctor result (2026-09-29):** RSS ok, web/Jina ok, V2EX ok, Bilibili ok, YouTube [!] (JS-runtime
  version check - metadata search works), GitHub [!] (gh not installed), Exa search [X]; 4/16.
- **Channels:**
  | Channel | Status | Backend | Why |
  |---|---|---|---|
  | `news_rss` | **enabled** | Google News RSS search (`news.google.com/rss/search`) | public, no key/cookie |
  | `youtube` | **enabled** | `yt-dlp --flat-playlist --dump-json "ytsearchN:..."` with `--no-cookies --no-cookies-from-browser --ignore-config` | public metadata, no login |
  | linkedin | disabled | - | forbidden by policy |
  | instagram, facebook | disabled | OpenCLI | need a logged-in browser session |
  | twitter_x | disabled | twitter-cli/OpenCLI | needs exported login cookies |
  | reddit | disabled | OpenCLI/rdt-cli | login required |
  | github | disabled | gh CLI | search needs `gh auth login` |
  | web_search_exa | disabled | Exa MCP via Node/mcporter | extra runtime; general search deferred with Brave |
  | web_reader_jina | disabled | r.jina.ai | Google News links are JS redirects (403); company sites covered by Phase 3 |
  | xiaohongshu, xueqiu, boss_zhipin, xiaoyuzhou, v2ex, bilibili | disabled | various | cookies/keys, or irrelevant to the ICP |

  The allowed set is fixed in code; config can only choose a subset. Forbidden hosts
  (LinkedIn, Instagram, Facebook, X/Twitter, Threads, TikTok, Reddit, ...) are dropped even if a
  feed returns them.
- **Configure (`.env`, no secrets involved):**
  | Variable | Default | Meaning |
  |---|---|---|
  | `AGENT_REACH_CHANNELS` | `news_rss,youtube` | subset of the allow-list |
  | `AGENT_REACH_YTDLP_PATH` | `yt-dlp` | yt-dlp from the Agent-Reach uv tool |
  | `AGENT_REACH_TIMEOUT_SECONDS` | `30` | per request / subprocess |
  | `SIGNAL_MAX_QUERIES_PER_COMPANY` | `5` | query budget |
  | `SIGNAL_MAX_ITEMS_PER_COMPANY` | `20` | processed-item budget |
- **Cost:** free public endpoints; no paid switch needed. It still reaches the internet, so it
  requires `PROVIDER_MODE=live` + `--live`.
- **Run:** dry run `uv run python scripts/run_signal_research.py`; tiny live run: set
  `PROVIDER_MODE=live` for the one command, then
  `uv run python scripts/run_signal_research.py --live --limit 1 --max-queries 2 --max-items 5`
  (see the runbook, section 9).
- **Update/remove:** `uv tool upgrade agent-reach` (then re-run doctor + tests) /
  `uv tool uninstall agent-reach`.

## Laya — qualification gate (Phase 5) ✅ live-capable (local)
- **Verified 2026-09-30:** PyPI `laya` **0.3.22** (Convai Innovations, Apache-2.0; homepage
  huggingface.co/convaiinnovations/laya). Contract read from the installed `laya/serve.py` and
  `laya/agent.py`: `GET /health`; `POST /v1/systemone` with `{state, model, questions}` where each
  question is `choice` (criteria label→description), `score` (ordered levels) or `noul`
  (criteria keyed `false`/`true`); answers carry `choice`/`score`/`noul`, `probabilities`,
  `confidence` and the calibrated `answer_confidence`.
- **Install (isolated, once):** `uv tool install "laya[serve]==0.3.22"` — torch 2.14 and
  transformers 5.17 live in the tool's own environment, not in this app.
- **Run (keep the window open):**
  `LAYA_HOST=127.0.0.1 LAYA_PORT=8765 LAYA_DEVICE=cpu LAYA_MODELS=typed-decisions LAYA_PRELOAD=1 laya-serve`
  (PowerShell: set each with `$env:NAME="value"` first). The first start downloads the
  checkpoint (~808 MB) into `%USERPROFILE%\.cache\huggingface`. Loaded revision here:
  `typed-decisions` @ `55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851`, CPU.
- **Configure the app:** `LAYA_ENDPOINT_URL=http://127.0.0.1:8765` (loopback only - other hosts are
  refused), `LAYA_CHECKPOINT=typed-decisions`, `LAYA_TIMEOUT_SECONDS=30`; `LAYA_API_KEY` only if you
  start the sidecar with its own `LAYA_API_KEY` (bearer). `LAYA_MODEL_PATH` (in-process) is not
  supported (ADR-040).
- **Health:** `curl.exe http://127.0.0.1:8765/health` → `{"status":"ok","loaded":["typed-decisions"],"device":"cpu",...}`.
- **Cost / speed:** free, local compute; ≈ 10–15 s per company (4 questions) on this CPU.
- **Stop / remove:** `Ctrl+C` in its window / `uv tool uninstall laya` (+ delete the HF cache
  folder `models--convaiinnovations--laya`).
- **Accuracy caveat:** zero-shot on our 28 synthetic cases its `noul` answers did not separate
  good from weak/namesake leads (see PHASE_5_COMPLETION §10). Calibrate or fine-tune on
  ~50–200 manually labeled real companies before relying on it for QUALIFIED decisions.

## enrichment-kit — enrichment waterfall (Phase 6) ✅ live-capable (local sidecar)
- **Verified 2026-10-01:** github.com/masteranime/enrichment-kit @ `6998898c038a1f61c900aa8d96290b93049d0b84`
  (tag v0.1.1; package.json 0.1.0; MIT; TypeScript). **Not on npm; no HTTP server mode**
  (library + CLI). Waterfall orders providers by cost/hit-rate (Hunter before Apollo), accepts
  results ≥ `minConfidence` (0.5), stops at `earlyExitOnConfidence` (0.85); Memory/File cache
  (30 d). Upstream `npm run build` fails type-checking `src/cli` with TypeScript 5.9 (TS18047).
- **Integration (ADR-047):** project-owned Node sidecar `sidecars/enrichment-kit` (127.0.0.1)
  imports a pinned, library-only local build (`node setup.mjs`: clone pinned commit, `npm install
  --ignore-scripts`, `tsc` without `src/cli`). Upstream files are not edited. The sidecar holds the
  Hunter/Apollo keys; the Python app never sends them.
- **Configure (.env):** `ENRICHMENT_KIT_BASE_URL=http://127.0.0.1:8787` (loopback only),
  `ENRICHMENT_KIT_API_TOKEN=<long random string>` (local bearer), `ENRICHMENT_KIT_PORT=8787`,
  `ENRICHMENT_KIT_MAX_PROVIDER_CALLS=50` (sidecar hard cap), `ENRICHMENT_KIT_TIMEOUT_SECONDS=45`.
- **Run:** `cd sidecars\enrichment-kit; node setup.mjs` (once); `npm test`; `node server.mjs`.
  Health: `curl.exe http://127.0.0.1:8787/health` (no credits). App readiness shows an
  `enrichment` check (config + sidecar reachability; never a paid call).
- **Deviations:** Apollo work email only (`reveal_personal_emails=false`, no phone); upstream
  cache off (PostgreSQL cache in the app); typed error kinds; per-process call cap.

## Hunter — email discovery + verification (Phase 6) ✅ live-capable (via sidecar)
- **Verified 2026-10-01 (hunter.io/api-documentation/v2):** auth by `api_key` query, `X-API-KEY`
  or `Authorization: Bearer`; Email Finder 1 credit **only when an email is found**; Domain Search
  1 credit when ≥ 1 result; Email Verifier 1 credit; statuses valid / invalid / accept_all /
  webmail / disposable / unknown; HTTP 202 = verification in progress, 222 = SMTP retry later,
  401 bad key, 403 rate limit, 429 usage limit, 451 PII opt-out; rate limits 15 req/s (finder,
  search) and 10 req/s (verifier); Account Information endpoint is free.
- **Used for:** Email Finder (through the enrichment-kit Waterfall; upstream sends the key in the
  query string), Email Verifier and Domain Search and Account usage (sidecar code; key in the
  `X-API-KEY` header).
- **Configure:** `HUNTER_API_KEY=<key>` in `.env` (read by the sidecar; the app checks presence).
- **Live (2026-10-01, Free plan):** 1 Email Finder + 1 Email Verifier. Hunter counters: searches
  used 0 → 1 (remaining 50 → 48), verifications used 0 → 3. The single finder call moved the
  verification counter by 2 - track spend with Hunter's own counters (the CLI prints them).
- **Rule:** a found email stays UNVERIFIED until a fresh Hunter verification says `valid`.
- **Phone (optional, default OFF):** the Email-Finder response may include a `phone_number`
  Hunter already holds. It is passed on only with `ENRICHMENT_PHONE_ENABLED=true` (sidecar
  request flag `includePhone`), costs no extra credit and is often absent - a blank phone is
  normal. See [ENRICHMENT_POLICY.md §3a](../phases/ENRICHMENT_POLICY.md).
- **LinkedIn URL:** Hunter / Apollo may return the person's public profile URL; it is stored
  with its provider as provenance. The LinkedIn site is never fetched.

## Apollo — people/company enrichment (Phase 6) ✅ live-capable (via sidecar)
- **Verified 2026-10-01 (docs.apollo.io):** `POST /api/v1/people/match` (People Enrichment):
  credits only on a match (email reveal 1; personal emails +1; phone +8, webhook required);
  `match_confidence: none` = no charge. `POST /api/v1/mixed_people/api_search`: 0 credits but
  needs a **master** key and returns obfuscated last names (not used). `GET
  /api/v1/organizations/enrich`: 1 credit (not used - the domain is already verified in Phase 3).
  Errors 401 / 403 (plan scope) / 422 / 429 (≈600 req/h, plan-dependent).
- **Used for:** People Match as the Waterfall fallback/complement, work email only
  (`reveal_personal_emails=false`, `reveal_phone_number=false`).
- **Configure:** `APOLLO_API_KEY=<key>` in `.env` (read by the sidecar).
- **Live (2026-10-01):** 1 People Match (Alpna Jain @ drivn.co.in) → work email found, Apollo
  `email_status=verified` (kept as evidence only; Hunter verification then returned `valid`).
  Apollo's credit balance is not readable through this key; per docs ≥ 1 credit.
- **Rule:** never called for non-QUALIFIED companies; never overwrites a higher-confidence Hunter
  email; its "verified" claim never counts as verification.
- **Phone reveal is NOT used:** Apollo charges mobile credits per reveal and delivers the number
  asynchronously to a webhook. The sidecar pins `reveal_phone_number=false` even when the phone
  capability is on; turning it on would need a public webhook and its own budget - a deliberate
  future decision, not a configuration switch.

## OpenAI — reasoning provider (Phase 10) ✅ live-verified
- **What:** lead analysis / offer angle through the `ReasoningProvider` boundary. Official
  `openai` SDK, Responses API, strict JSON-schema output, no tools.
- **Obtain:** an API key from the OpenAI dashboard (prefer a restricted project key + a monthly
  limit on the project).
- **Configure (`.env`):** `OPENAI_API_KEY` (secret), `OPENAI_MODEL=gpt-6-luna`,
  `OPENAI_MAX_TOKENS=2500`, `OPENAI_MAX_RETRIES=2`, `OPENAI_TIMEOUT_SECONDS=60`,
  `OPENAI_MAX_TOKENS_PER_RUN=60000`, `OPENAI_MAX_ESTIMATED_COST_USD_PER_RUN=0.25`,
  `OPENAI_MAX_ESTIMATED_COST_USD_PER_MONTH=5.00`; optional `OPENAI_BASE_URL`,
  `OPENAI_REASONING_EFFORT`, `OPENAI_PRICE_INPUT_PER_1M_USD`, `OPENAI_PRICE_OUTPUT_PER_1M_USD`.
- **Check:** `uv run python scripts/check_openai.py` (free) · `--chat --analysis-sample` (paid,
  about $0.001, needs the per-process live switches).
- **Guards:** paid guard → key → price known → worst-case cost within budget → model exists.
- **Details, errors, rotation:** [../OPENAI_PROVIDER_NOTES.md](../OPENAI_PROVIDER_NOTES.md).

## Techno-AI-Router — DEPRECATED since Phase 10 (kept for explicit opt-in only)
> Not used by default and not required for startup or readiness. Select it only with
> `REASONING_PROVIDER=ai_router`. Historical notes below are unchanged.

- **What:** TechnoTribes' model router at `https://techno-ai-router.vercel.app` (project default).
  One `tair_` key gives access to many models; no OpenAI/Anthropic/Google/OpenRouter keys needed.
- **Obtain:** a key with LLM + `openrouter` gateway scope from the router owner (console → API
  Keys; the console address is not public). Beginner steps:
  [../TECHNO_AI_ROUTER_SETUP_GUIDE.md](../TECHNO_AI_ROUTER_SETUP_GUIDE.md).
- **Configure (`.env`):** `AI_ROUTER_API_KEY` (secret). Optional: `AI_ROUTER_BASE_URL` (blank =
  default), `AI_ROUTER_GATEWAY=openrouter`, `AI_ROUTER_DEFAULT_MODEL` (blank = `recommended`
  free model from the live list), `AI_ROUTER_FALLBACK_MODELS` (`gateway/model`, comma-separated),
  `AI_ROUTER_TIMEOUT_SECONDS=60`, `AI_ROUTER_MAX_RETRIES=1`, `AI_ROUTER_MAX_TOKENS=1500`,
  `AI_ROUTER_TEMPERATURE=0.2`, `AI_ROUTER_MAX_PRICE_PER_1M_TOKENS_USD=0` (free only).
- **Contract (verified):** discovery `GET /health`, `/v1/providers?enabled=true`,
  `/v1/models?provider=&modality=llm` (no key); `POST /v1/chat/completions` with
  `Authorization: Bearer`, body `{gateway, model, messages, fallback, temperature, maxTokens}`,
  answer `choices[0].message.content`, `usage`, `x_router{gateway, provider}`; errors 400/401/
  403/429/5xx as `{"message","error","statusCode"}`.
- **Guards:** paid-guarded (`PROVIDER_MODE=live` + `ALLOW_PAID_PROVIDER_CALLS=true`, per process);
  https only (loopback excepted); key never logged. Check: `uv run python scripts/check_ai_router.py`.
- **Phase 5 review path:** still disabled (`review()` not implemented on the live adapter); the
  mock reviewer never decides (ADR-044).

## Google Sheets — output projection (Phase 8) ✅ live-capable
- **What:** the official lead output. A projection of PostgreSQL; edits in the Sheet are never
  read back. Not a lead-data provider (not in the provider registry).
- **Auth:** service account (the pre-Phase-5 approach): key FILE at
  `GOOGLE_SERVICE_ACCOUNT_JSON` (default `secrets/google/service-account.json`, git-ignored),
  read only by `google-auth`; scope `spreadsheets`. Share the Sheet with the service-account
  `client_email` as Editor. Beginner steps: [../GOOGLE_SHEETS_SETUP_GUIDE.md](../GOOGLE_SHEETS_SETUP_GUIDE.md).
- **Configure (`.env`):** `GOOGLE_SHEETS_SPREADSHEET_ID` (stored/printed masked),
  `GOOGLE_SHEETS_WORKSHEET=Leads`, `GOOGLE_SHEETS_REVIEW_WORKSHEET=Review_Queue`,
  `GOOGLE_SHEETS_RUNS_WORKSHEET=Export_Runs`, `GOOGLE_SHEETS_TIMEOUT_SECONDS=30`,
  `GOOGLE_SHEETS_MAX_RETRIES=2`, `EXPORT_MAX_ROWS_PER_RUN=50`, `ALLOW_SHEETS_WRITES=false`.
- **API used (v4):** spreadsheet metadata, `batchUpdate addSheet`, `values.get`,
  `values.batchUpdate` (RAW). 401/403 → share the Sheet; 404 → wrong id; 429/5xx → bounded retry.
- **Cost:** free (Sheets API quota only). Verified 2026-10-01 against workbook "Phase4_Test".

## PostgreSQL
- **Dev:** `docker run -d --name lead-intel-pg -e POSTGRES_PASSWORD=<choose> -e POSTGRES_DB=lead_intel -p 5432:5432 postgres:16`
- **Configure:** `DATABASE_URL=postgresql+psycopg://postgres:<password>@localhost:5432/lead_intel`
- **Required** when `APP_ENV` is `staging` or `production` (config validation + readiness 503).
- **Migrations:** `uv run alembic upgrade head` (current head: `0006`). Readiness reports
  `database_schema: warning` when the DB is behind.
- **Tests** use a separate database `lead_intel_test` (auto-created; override with
  `TEST_DATABASE_URL`). They never touch `lead_intel`.
