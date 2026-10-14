# Architecture Decisions

Format: context → decision → alternatives considered → consequences. Status is `Accepted`
unless noted. Newest decisions are appended at the bottom.

---

## ADR-001 Adopt the Phase 1 target structure (greenfield), with small deviations
**Date:** 2026-09-24 · **Phase:** 1

**Context.** The repository audit found only three PDFs, so there was no existing structure to
preserve.

**Decision.** Use the build prompt's `app/…`, `tests/…`, `docs/…`, `scripts/` layout, with these
deviations:
- **One changelog** at `docs/CHANGELOG.md` (the mandatory location). No duplicate root
  `CHANGELOG.md`; the README links to it.
- **Provider DTOs live in `app/providers/contracts.py`**, not `app/schemas/`. `app/schemas/` is
  reserved for HTTP API DTOs. Keeps provider contracts next to the protocols that use them.
- **`app/utils/` holds only `network_guard.py`** for now; no speculative helpers.
- **The three source PDFs stay at the repo root, untouched.**

**Alternatives.** Moving PDFs to `docs/reference/` (rejected: they are inputs owned by the user
and referenced by name in the handoff process).

---

## ADR-002 uv + `pyproject.toml` + lockfile; Python 3.13 pinned, `>=3.12` floor
**Context.** No dependency management existed. `uv` was already installed.

**Decision.** `uv` manages the venv and `uv.lock`; `.python-version` pins 3.13. Minimum
`requires-python = ">=3.12"` (needed for PEP 695 generics used in repositories).

**Alternatives.** Poetry (slower, extra tool), pip + requirements.txt (no lock of transitive
deps). Python 3.14 (uv's default on this machine) rejected for now because ML/browser wheels
needed in Phases 3/5 (Playwright for Crawl4AI, PyTorch for Laya) typically lag new CPython
releases.

**Consequences.** `uv sync` reproduces the exact environment.

---

## ADR-003 FastAPI + Pydantic v2 + pydantic-settings
**Decision.** FastAPI for the HTTP layer (blueprint recommendation), Pydantic v2 for domain
contracts and DTOs, pydantic-settings for typed config.

**Alternatives.** Dataclasses/attrs for domain (rejected: we need validation at every provider
boundary anyway, and Pydantic gives JSON (de)serialization for LLM/Laya payloads for free).

---

## ADR-004 Capability-based provider protocols + a single registry
**Context.** External services must be replaceable, and two vendors (Firecrawl, Crawl4AI) must
share one interface.

**Decision.** Define *capabilities* (`DiscoveryProvider`, `WebsiteIntelligenceProvider`,
`SignalResearchProvider`, `QualificationProvider`, `EnrichmentOrchestrator`,
`EnrichmentVendor`, `EmailVerifier`) as `typing.Protocol`s. Each vendor ships `mock.py` and
`adapter.py`. `ProviderRegistry` is the only place that picks implementations.

**Alternatives.** ABC inheritance per vendor (more coupling), dependency-injection framework
(overkill for 8 providers).

**Consequences.** Swapping a vendor = one line in `registry.py`. Business logic never imports a
vendor module.

---

## ADR-005 Two-level safety switch: `PROVIDER_MODE` + `ALLOW_PAID_PROVIDER_CALLS`
**Context.** Paid credits must never be spent by accident (tests, dev runs, misconfig).

**Decision.**
- `PROVIDER_MODE=mock` (default): registry returns offline mocks.
- `ALLOW_PAID_PROVIDER_CALLS=false` (default): every live adapter for a paid provider raises
  `ProviderCallBlockedError` **before** touching credentials or the network.
- **enrichment-kit is classified as paid** because it spends Hunter/Apollo credits.
- Guard order: paid guard → credential check → call.

**Consequences.** Spending credits requires two explicit changes in `.env`. Readiness endpoint
reports both switches.

---

## ADR-006 Live adapters in Phase 1 are non-operational stubs; no vendor SDKs installed yet
**Decision.** Live adapter methods run the guard then raise `ProviderNotImplementedError`
(carrying the phase number). No vendor SDK (apify-client, firecrawl, crawl4ai, …) is added to
dependencies until its phase.

**Alternatives.** Write HTTP clients now against assumed APIs (rejected: APIs must be verified
against current docs in their phase; unused deps add risk and install weight — Crawl4AI pulls
Playwright/Chromium).

---

## ADR-007 enrichment-kit runs as a sidecar service (proposed — finalize in Phase 6)
**Status:** Accepted in Phase 6 (2026-10-01) — superseded in detail by ADR-047.

**Context.** enrichment-kit is a TypeScript library/CLI; it cannot be imported by Python.

**Decision (proposed).** Run it as a small internal Node service (`ENRICHMENT_KIT_BASE_URL`)
configured with HunterProvider → ApolloProvider. The Python `EnrichmentKitAdapter` is the only
enrichment entry point for workflows. Python `HunterAdapter`/`ApolloAdapter` are kept for direct
email verification and as a fallback if the sidecar approach is rejected.

**Alternatives.** Shelling out to the CLI per lead (simpler, slower, harder to cache);
re-implementing the waterfall in Python (duplicates a maintained library). Decide in Phase 6
after checking enrichment-kit's current packaging.

---

## ADR-008 Pydantic domain models separate from ORM; tables deferred
**Decision.** Domain models are Pydantic and framework-free. SQLAlchemy 2 + Alembic are wired
(lazy engine, naming convention, `env.py` reading `DATABASE_URL`) but **no tables or
migrations** exist yet. The first phase that persists an entity adds its ORM table, a
repository implementation and a migration.

*Phase 2 update:* the first tables now exist (ADR-024). Lead/Founder/Signal/Qualification
tables remain deferred to the phases that first write them.

**Alternatives.** Create the full schema now (blueprint suggestion) — rejected: fields will
change once real provider payloads are seen in Phases 2–6; migrating an unused schema is churn.
SQLModel (couples domain to DB).

**Consequences.** App starts and tests run with no database. Readiness shows
`database: not_configured` (error in staging/production).

---

## ADR-009 Business invariants live in the domain layer
**Decision.** Encode the pipeline's safety rules as model validation / state machine
(see ARCHITECTURE.md §4): no enrichment without qualification, no ENRICH after a failed hard
rule, discovery ≠ verification, signals must cite sources.

**Consequences.** Later orchestration code cannot violate these rules even by mistake.

---

## ADR-010 Standard-library logging with a JSON formatter and redaction
**Decision.** Stdlib `logging` + small JSON/console formatters, request-id context var,
`RedactionFilter` (secret-named keys + exact configured secret values).

**Alternatives.** structlog (nice, but an extra dependency for little gain at this size).

---

## ADR-011 Error taxonomy and single JSON error envelope
**Decision.** `AppError` subclasses carry `code`, `http_status`, `retryable`. All API errors
return `{"error": {code, message, details, request_id}}`. Unhandled exceptions return a generic
500 with no internals. `ProviderUnavailableError.retryable=True` prepares Phase 9 retry policy.

---

## ADR-012 Outbound network is blocked in tests and the smoke check
**Decision.** `app/utils/network_guard.py` patches socket connect/DNS to reject non-loopback
targets. It is autouse in the test suite; the smoke check also asserts zero attempts.

**Alternatives.** `pytest-socket` (blocks socket creation entirely, which breaks the Windows
asyncio event loop that uses a loopback socketpair).

---

## ADR-013 Environment-variable names
**Decision.** `APIFY_TOKEN` (not `APIFY_API_TOKEN` from the example list) because it is the
variable name used by Apify's own SDK/CLI tooling. *Phase 2 update:* retained; the token is
passed explicitly to `ApifyClientAsync(token=...)` from settings, so the name is ours to choose. `FIRECRAWL_API_KEY`,
`HUNTER_API_KEY`, `APOLLO_API_KEY`, `DATABASE_URL`, `AI_ROUTER_BASE_URL`, `AI_ROUTER_API_KEY` as
proposed. Keyless tools get endpoint/path variables (`CRAWL4AI_BASE_URL`, `LAYA_MODEL_PATH`,
`LAYA_ENDPOINT_URL`, `ENRICHMENT_KIT_BASE_URL`). Agent-Reach variables are deliberately
undefined until Phase 4.

**Consequences.** `tests/unit/test_repo_hygiene.py` fails if `.env.example` and `Settings` drift.

---

## ADR-014 Funding cap is evaluated in INR only (for now)
**Decision.** `FUNDING_CAP_INR=50000000` (₹5 Cr). `FundingDetails.within_cap` returns `None`
(unknown) for missing amounts or non-INR currencies instead of guessing. FX conversion is
deferred to the qualification phase.

---

## ADR-015 App factory instead of a module-level `app`
**Decision.** Run with `uvicorn app.main:create_app --factory`. Importing any module never reads
the environment, so tests build isolated apps with explicit settings.

---

## ADR-016 Async provider interfaces, sync DB engine (for now)
**Decision.** Provider methods are `async` (network-bound, will fan out). The DB engine is sync
(psycopg 3) — simpler, and FastAPI runs sync endpoints in a threadpool. Revisit if Phase 8/9
needs high DB concurrency (psycopg 3 supports async with the same driver).

---

## ADR-017 `httpx2` for the Starlette/FastAPI TestClient
**Context.** The installed Starlette emits a deprecation warning when the TestClient uses
`httpx` and recommends `httpx2`.

**Decision.** Dev dependency on `httpx2`; plain `httpx` removed. Tests run with
`-W error::DeprecationWarning` cleanly.

---

# Phase 2 decisions (2026-09-28)

## ADR-018 Official `apify-client` v3 (async), typed `Run` read duck-typed
**Context.** Apify recommends its Python client. v3.2.1 (verified 2026-09-28) returns Pydantic
models, but the model classes live in a private module (`apify_client._models`).

**Decision.** Use `ApifyClientAsync` and `actor(id).call(max_items=..., max_total_charge_usd=...,
run_timeout=..., wait_duration=..., logger=None)`, then `dataset(id).iterate_items(limit=...)`
(yields plain dicts). The adapter never imports private classes: it reads `Run` attributes in one
function (`ApifyAdapter._run_info`). The adapter depends on a small structural `ApifyClientLike`
protocol, so tests inject a fake client and never touch the network.

**Alternatives.** Raw REST via httpx (re-implements retries/pagination the client already
provides); Apify SDK (for code running *inside* Actors — not our case).

**Consequences.** A client major-version bump is contained to `adapter.py`. Pinned `<4`.

**Live finding.** Pay-per-event charges can settle *after* the run reports SUCCEEDED (the page
event was missing on the first read). The adapter re-reads the run (`run(id).get()`) after the
dataset read and keeps the settled `usage_total_usd` / `charged_event_counts`.

---

## ADR-019 Discovery Actor: `apify/google-search-scraper` for funding-news discovery
**Context.** Phase 2 needs one small, cheap, compliant Actor aligned with the primary ICP signal
(recent funding ≤ ₹5 Cr, India). No concrete source list was provided (Phase 1 open decision).

**Decision.** Use Apify's own maintained Google Search Results Scraper with funding-news queries
(default: `startup raises seed funding crore India`, country `in`). One dataset item = one
results page (~10 organic results). Verified via the Apify API on 2026-09-28: input schema
(build `lNbEbWu4zO04U9Qdw`), pay-per-event pricing (FREE tier: `actor-start` $0.001,
`search-page-scraped` $0.0045), output shape (`searchQuery`, `organicResults[]`).

**Alternatives.** Crunchbase/Tracxn/LinkedIn scraping Actors (third-party maintenance, higher
cost, platform-terms risk — excluded by the phase non-goals); news-site-specific crawlers (would
duplicate Firecrawl's Phase 3 role).

**Consequences.** Candidates are *company names parsed from news headlines* with the article as
evidence; domains are unknown until Phase 3. Headline parsing is heuristic and conservative
(skips when unsure). Precision is re-checked by website intelligence and the qualification gate.

---

## ADR-020 Actor-spec layer isolates Actor-specific JSON
**Decision.** `app/providers/apify/actors.py` holds one `ActorSpec` per Actor:
`build_input(query)` and `normalize(items) -> CandidateRecord[]`. Input keys are checked against
an allow-list captured from the verified schema; unknown request params are rejected; every paid
add-on and storage side effect is **pinned off explicitly** (notably `saveHtmlToKeyValueStore`,
whose Actor default is `true`, and `maximumLeadsEnrichmentRecords=0`). Unregistered Actor ids are
refused ("verify before use").

**Consequences.** Adding an Actor = adding a spec + tests; service/DB code is unchanged.

---

## ADR-021 Deduplication identities, enforced by the database
**Decision.**
- Company key: `domain:<canonical domain>` when known, else `name:<normalized name>` (NFKC,
  lowercase, punctuation stripped, legal suffixes like *Pvt Ltd* removed). Unique `dedupe_key`.
- Source key: SHA-256 of the canonical URL (lowercased host, no fragment/default port/tracking
  params, sorted query, no trailing slash). Unique `url_hash`.
- Every candidate writes a `discovery_hits` row (run × company × source, unique) so provenance is
  kept even for already-known companies.
- Upserts use `INSERT … ON CONFLICT … RETURNING (xmax = 0)` — atomic under concurrency (tested
  with 6 parallel writers).

**Known limitation.** A company first seen by name and later by domain gets two rows. Merging is
deferred to Phase 3, which resolves company domains.

---

## ADR-022 Spend controls: two-layer cost cap, request can only lower ceilings
**Context (revised after the first live attempt).** Apify rejects `max_total_charge_usd` below
**$0.50** (HTTP 400 "Maximum cost per run is less than the allowed minimum of $0.50", observed
2026-09-28; the first two live attempts failed at validation — no run started, nothing charged).
A $0.50 cap alone is ~90× the expected cost of a 1-page run, too loose as a budget control.

**Decision.** Two layers:
1. **Pre-flight estimate guard** — the Actor spec computes the worst-case cost of the *exact*
   input (actor-start + queries × pages × highest-tier page price; paid add-ons are pinned off).
   If it exceeds `APIFY_MAX_ESTIMATED_COST_USD_PER_RUN` (default **$0.05**) the run is blocked
   with `ProviderCallBlockedError` before Apify is called. The estimate is stored per run
   (`discovery_runs.estimated_cost_usd`, migration `0002`).
2. **Apify-enforced hard cap** — `APIFY_MAX_CHARGE_USD_PER_RUN` (default and minimum **$0.50**)
   passed as `max_total_charge_usd`; requests below $0.50 are rejected locally with a clear
   message.

Other ceilings: `APIFY_MAX_ITEMS_PER_RUN` (default 1), `APIFY_RUN_TIMEOUT_SECONDS` (default 180).
Apify API error messages are now surfaced (truncated, token-scrubbed) so failures are diagnosable.

**Original text:** `APIFY_MAX_ITEMS_PER_RUN` (default 1), `APIFY_MAX_CHARGE_USD_PER_RUN` (default
0.05), `APIFY_RUN_TIMEOUT_SECONDS` (default 180) are hard ceilings. A request's `max_items` /
`max_charge_usd` are clamped with `min(request, ceiling)`. Both are passed to Apify
(`max_items`, `max_total_charge_usd`), the dataset read is capped at `max_items`, and the run is
recorded with the effective limits, provider run id, cost (`usage_total_usd`) and charged events.

---

## ADR-023 No HTTP endpoint that triggers paid discovery (yet)
**Context.** The API has no authentication.

**Decision.** Discovery is triggered only via `scripts/run_discovery.py`. A live run needs three
explicit opt-ins: `--live`, `PROVIDER_MODE=live`, `ALLOW_PAID_PROVIDER_CALLS=true`, plus a
database at the migration head. Without `--live` the script is a dry run (mock provider,
in-memory store).

**Alternatives.** `POST /discovery/runs` now (rejected: anyone reaching the port could spend
credits). Revisit with authentication in the output/dashboard or operations phases.

---

## ADR-024 Minimum Phase 2 schema; separate test database
**Decision.** Migration `0001` creates `discovery_runs`, `companies`, `sources`,
`discovery_hits` only. ORM rows (`app/db/models.py`) are separate from domain models; the
repository maps between them. `DiscoveryRun` is a new domain model (richer than the generic
`PipelineRun`, which stays for Phase 9 multi-stage runs). PostgreSQL tests run against
`lead_intel_test` (auto-created) and assert migration up/down reproducibility and zero
model↔migration drift. Readiness now reports `database_schema` (warning when not at head).

---

# Phase 3 decisions (2026-09-29)

## ADR-025 Firecrawl via its REST v2 API (httpx2), not the SDK
**Context.** The current SDK is the `firecrawl` package 4.45.0 (the old `firecrawl-py` name
stopped at 1.17.0). It depends on `nest-asyncio` (patches the asyncio event loop globally),
`aiohttp`, `requests`, `websockets`. We need three endpoints: `/v2/search`, `/v2/map`,
`/v2/scrape` (+ free `/v2/team/credit-usage`).

**Decision.** Call the documented REST API directly with `httpx2` inside `FirecrawlAdapter`.
Request/response shapes verified against docs.firecrawl.dev on 2026-09-29. Vendor payloads never
leave the adapter.

**Alternatives.** Official SDK (rejected: global event-loop patching + 4 extra HTTP stacks).

**Consequences.** Endpoint changes are handled in one file; tests use `httpx2.MockTransport`.

---

## ADR-026 Crawl4AI runs as the official Docker server on 127.0.0.1
**Context.** Crawl4AI 0.9.4's *base* install pulls Playwright/Patchright + Chromium, a litellm
fork, numpy, nltk, shapely and more; 0.9.4 also shipped SSRF-related security fixes.

**Decision.** Run `unclecode/crawl4ai:0.9.4` (pinned) as container `lead-intel-crawl4ai`, port
bound to **127.0.0.1:11235 only**, `--shm-size=1g`, restart unless-stopped. Verified: server is
secure by default (401 without token); a locally generated token (`CRAWL4AI_API_TOKEN`, kept in
`.env`) authenticates us. The adapter talks HTTP (`/crawl`). No optional extras (torch,
transformer) installed.

**Alternatives.** In-process library (rejected: heavy deps in the app venv, browser in our
process); remote hosted Crawl4AI (none needed).

**Consequences.** Image ≈ 6.1 GB on disk. Fallback is unavailable when the container is down
(readiness shows `crawl4ai operational=false` when `CRAWL4AI_BASE_URL` is unset).

---

## ADR-027 General web search is its own capability (`WebSearchProvider`)
**Decision.** New protocol `WebSearchProvider.search_web(WebSearchQuery) -> WebSearchResult`,
registry attribute `web_search`, backed by Firecrawl Search in Phase 3. Brave (deferred) or any
other engine can replace it without touching the domain-resolution logic.

---

## ADR-028 Domain resolution: conservative scoring + homepage verification, REVIEW over guessing
**Decision.** Query `"<name>" official website India` (≤5 results = 2 credits). Exclude social
platforms (kept as metadata), directories/news/app stores, and the publishers of the company's
own discovery sources. Score candidates (domain-label ↔ name match is required to reach the
threshold; title/description/position add evidence). RESOLVED only if score ≥
`DOMAIN_MIN_CONFIDENCE` (0.7) and the margin over the runner-up ≥ 0.15; otherwise REVIEW
(nothing crawled) or NOT_FOUND. After crawling, the **homepage must name the company**
(alphanumeric match); otherwise REVIEW and the domain is not attached. Every attempt, with all
scored/excluded candidates, is stored in `domain_resolutions`.

---

## ADR-029 Identity and safe merge policy
**Decision.** `companies.domain` is written only after verification and is unique (partial
unique index). `dedupe_key` (first-seen identity) is never rewritten. If a second company
verifies to an already-owned domain, it is merged **into** the owner in one transaction:
discovery hits, pages, crawls, resolutions re-pointed (exact duplicates dropped), counters
combined, and a `company_merges` row records the evidence. That row doubles as an **alias**:
discovery upserts consult it (and the verified domain) first, so a merged name is never
re-created. Merges never happen on name similarity alone.

---

## ADR-030 Crawl budgets and page selection
**Decision.** Per company: `WEBSITE_MAX_PAGES_PER_COMPANY=8`,
`FIRECRAWL_MAX_CREDITS_PER_COMPANY=15` (search 2 + map 1 + ≤8 pages = 11 worst case); per run:
`FIRECRAWL_MAX_CREDITS_PER_RUN=50` (a `--max-credits` flag can only lower it). Firecrawl `map`
(1 credit) finds real URLs first, because scraping guessed paths is billed even when the page is
a 404. Pages chosen by priority home → about → products → careers → pricing → blog → contact →
one extra (shortest path per type). Scrape requests never ask for JSON extraction (+4) or PDF
parsing (+1). The adapter reduces pages to fit the credit cap and refuses when even the homepage
does not fit.

---

## ADR-031 URL policy: social never fetched, internal names never crawled
**Decision.** LinkedIn/Instagram/X/Facebook/YouTube/etc. URLs are never crawled in Phase 3; they
are collected as `social_links` metadata. Directory/news/app-store/data-broker hosts are never
treated as official sites. `crawlable_domain` refuses `.local/.localhost/.internal/.test/
.invalid/.lan` names; IP literals are already rejected by domain normalization (SSRF hygiene).

---

## ADR-032 `httpx2` is the runtime HTTP client
**Decision.** Promote `httpx2` (already required by Starlette's TestClient, same API as httpx)
from a dev to a runtime dependency; both Firecrawl and Crawl4AI adapters use it. No second HTTP
stack was added.

---

# Phase 4 decisions (2026-09-29)

## ADR-033 Agent-Reach is used as a routing/installation layer, not a library
**Context.** Upstream docs (verified 2026-09-29) state Agent-Reach 1.5.0 "does not expose a
Python API": it installs upstream CLIs and a SKILL.md so an agent calls them directly.

**Decision.** Install Agent-Reach (pinned commit `a19a171`) as an isolated `uv tool` and use its
doctor output + channel routing as the reference. `AgentReachAdapter` calls the same upstream
backends for the permitted channels: Google News RSS (Agent-Reach RSS channel) and yt-dlp
(Agent-Reach YouTube channel, installed inside the tool environment). Vendor parsing stays in the
adapter behind `SignalResearchProvider`.

**Alternatives.** Importing Agent-Reach into the app venv (no API to import; would add loguru,
rich, requests, yt-dlp to the app); `agent-reach install --system` (installs Node/gh/Playwright
system-wide - rejected); a Docker sidecar (no official image; heavier than a uv tool).

**Consequences.** Project venv gains only `defusedxml`. Upgrading Agent-Reach = `uv tool upgrade`
+ re-run doctor + tests.

---

## ADR-034 Public/unauthenticated channel allow-list enforced in code
**Decision.** `ResearchChannel` has only `news_rss` and `youtube`. `AGENT_REACH_CHANNELS` can only
select a subset (validator rejects anything else). `DISABLED_CHANNELS` records every other
Agent-Reach channel with its reason. Research-time host policy `FORBIDDEN_RESEARCH_HOSTS`
(LinkedIn, Instagram, Facebook, X/Twitter, Threads, TikTok, Reddit, ...) drops findings in the
adapter and again in the service. yt-dlp always runs with `--no-cookies
--no-cookies-from-browser --ignore-config`; the RSS client sends no cookies.

**Note.** This research policy is narrower than the Phase 3 *crawl* policy (which also never
crawls YouTube/GitHub pages): Phase 4 may read public YouTube metadata.

---

## ADR-035 Vendor-neutral relevance, classification and confidence rules
**Decision.** `app/domain/signals.py`: a finding is kept only if it names the company (whole
word; compact match for multi-word names) and matches at least one keyword rule (funding,
AI interest, hiring, expansion, product/customer, pain point). One finding may yield several
signal types. Confidence = channel base (news 0.55, YouTube 0.4) + 0.15 when verified-website
context terms appear + 0.15 when the verified domain appears + 0.05 for several cues, capped at
0.95. Evidence = the verbatim headline/title; nothing is inferred. New `SignalType.PRODUCT_CUSTOMER`.

**Alternatives.** LLM classification (Phase 7 / AI Router - out of scope).

---

## ADR-036 VERIFIED-only research gate with defence in depth
**Decision.** `SignalResearchService` researches only companies with
`domain_status=verified` and a domain; others produce a SKIPPED run and no provider call. The
service re-checks provider output: query/item budget (violation → FAILED, nothing persisted),
permitted channel, forbidden hosts. Research never mutates company identity (only
`signals_researched_at`).

---

## ADR-037 Signal persistence: reuse `sources`, new `signals` + `signal_research_runs`
**Decision.** Migration `0004`: `signal_research_runs` (budgets, channels used/disabled, query
log, counts, errors) and `signals` (company, source, run, type, summary, verbatim evidence,
confidence with DB CHECK 0..1, channel, platform, query, published_at, evidence notes; unique
company+type+source). Evidence URLs go into the existing deduplicated `sources` table
(provider `agent_reach`). Signals are built through the `Signal` domain model first.

**Known limitation.** Dedupe is per source URL: the same event reported by several publishers
yields several signals (event-level clustering deferred to Phase 5/7).

---

## ADR-038 Per-query failure isolation and typed research errors
**Decision.** New error types: `ProviderTimeoutError`, `ProviderRateLimitedError` (retryable),
`ProviderMalformedResponseError`, `ProviderChannelUnsupportedError`,
`ProviderPolicyBlockedError`. A failing query is logged in the run's query log and the run
continues; the run fails only when every query failed. Empty feeds are `empty`, not errors.
RSS is parsed with `defusedxml` (entity-expansion/XXE refused).

---

# Phase 5 — Qualification gate (2026-09-30)

## ADR-039 Deterministic hard rules first; semantic stages can only be more cautious
**Decision.** Qualification runs in a fixed order: identity → hard ICP rules (on clustered
events) → evidence bundle → Laya → optional LLM review (REVIEW only) → policy. Identity and
geography failures are final. Laya/LLM may lower an outcome freely but may produce QUALIFIED
only when `HardRuleEvaluation.qualified_allowed` is true. The database repeats the key
invariant (`decision='qualified'` requires `hard_rules_passed`).
**Why.** Business rules (₹5 Cr cap, 90-day windows, verified identity) are contractual; a model
must not be able to talk past them. **Alternatives.** Weighted score blending rules and model
outputs (rejected: a high model score could offset a failed rule).

## ADR-040 Laya as an isolated, loopback-only `laya-serve` sidecar
**Decision.** Verified upstream `laya` 0.3.22 (PyPI, Convai Innovations, Apache-2.0) from its
installed source (`serve.py`, `agent.py`). Installed as `uv tool install "laya[serve]==0.3.22"`
(torch 2.14 / transformers 5.17 stay out of the app lock), run with
`LAYA_HOST=127.0.0.1 LAYA_PORT=8765 LAYA_MODELS=typed-decisions LAYA_DEVICE=cpu`. The app talks
HTTP (`POST /v1/systemone`, `GET /health`) through `LayaAdapter`; non-loopback endpoints are
refused so evidence cannot leave the machine by misconfiguration. `LAYA_MODEL_PATH` (in-process)
is not implemented. **Alternatives.** In-process `laya.load()` (rejected: ~2 GB of ML deps in
the app env); hosted inference (rejected: cost + data egress).

## ADR-041 Event clustering before scoring; provenance never deleted
**Decision.** Signals are grouped into events (per source URL, then headline Jaccard ≥ 0.5 /
same round amount / shared significant number + 2 key words, within 7 days). Rules, the bundle
and Laya see events; publisher count is shown but never adds confidence. Every signal id stays
inside its event and in the persisted bundle. **Why.** Phase 4 found one ₹45 Cr round reported
four times; counting coverage would inflate qualification.

## ADR-042 Missing / cumulative / foreign-currency amounts are UNKNOWN, never a pass
**Decision.** Round amounts need an explicit unit; valuation/revenue/share-price figures are
ignored; cumulative totals are flagged and never used as the round; USD converts only with an
explicit `ICP_USD_TO_INR_RATE`. UNKNOWN routes to REVIEW. Recent above-cap rounds on an
intent-qualified company follow `ICP_OVER_CAP_FUNDING_POLICY` (default `review`).
**Why.** The prompt requires that a missing amount never passes the funding path and that
lifetime funding is not treated as the latest round.

## ADR-043 Immutable qualification history (migration 0005)
**Decision.** `qualification_runs` + `qualification_decisions` (hard-rule results, reasons,
Laya result/confidence/model, optional LLM review, score, policy version, ICP snapshot,
evidence bundle, evidence signal ids). Decisions are insert-only: a PL/pgSQL trigger rejects
UPDATE; re-qualification inserts a new row; "latest" is by `decided_at`. **Alternatives.** A
status column on `companies` (rejected: overwrites history).

## ADR-044 LLM review behind a provider protocol, disabled by default
**Decision.** `LlmReviewProvider` (+ `ProviderName.AI_ROUTER`, paid, credential
`AI_ROUTER_API_KEY`). No AI Router API contract or credentials exist, so the live adapter is a
guarded non-operational contract (paid guard → credentials → not implemented); a deterministic
mock never decides. The service calls a reviewer only for REVIEW cases and only with
`LLM_REVIEW_ENABLED=true`; reviews are accepted only above `LLM_REVIEW_MIN_CONFIDENCE`, citing
evidence ids present in the bundle, and never to reach a QUALIFIED the hard gate forbids.

## ADR-045 The only door into Phase 6
**Decision.** `app.services.enrichment_gate.ensure_enrichment_allowed(store, company_id)` passes
only for a company whose latest decision is QUALIFIED with passed hard rules under the current
`POLICY_VERSION`; otherwise `EnrichmentNotAllowedError`. Phase 5 modules import no enrichment,
discovery or crawling vendor (AST test).

## ADR-046 Labeled evaluation set kept separate; live Laya kept conservative
**Decision.** 28 synthetic labeled cases in `evaluation/qualification/labeled_cases.json`
(13 required categories + identity/geography), separate from unit fixtures and production data.
Live Laya on this set: 21/28 decisions match, **0 false QUALIFIED**; its `noul` answers
(fit 0.57–0.69, attribution 0.48–0.68) did not separate positives from namesake/weak cases, so
thresholds stay 0.7/0.3 (not tuned on synthetic data) and hard-rule-passing companies land in
REVIEW with live Laya. **Revisit** with ≥ 50–200 manually labeled real companies (calibration or
fine-tuning of the typed-decisions checkpoint).

---

# Phase 6 — Enrichment (2026-10-01)

## ADR-047 Project-owned Node sidecar around a pinned enrichment-kit build
**Context.** Verified 2026-10-01: enrichment-kit (masteranime/enrichment-kit, MIT) is a
TypeScript library + CLI, not on npm, no HTTP server; `npm run build` fails on `src/cli`.
**Decision.** `sidecars/enrichment-kit`: `setup.mjs` clones commit `6998898c03`, installs with
`--ignore-scripts`, builds library-only (generated tsconfig excluding `src/cli`; upstream files
untouched); `server.mjs` binds 127.0.0.1 and exposes `/health`, `/v1/enrich` (upstream
Waterfall), `/v1/verify`, `/v1/people`, `/v1/account`. Local bearer token; per-process call cap;
never logs bodies. **Alternatives.** Re-implement the waterfall in Python (duplicates the
library); shell out to the CLI per lead (the CLI does not build; slow); fork upstream (not
necessary).

## ADR-048 The sidecar is the only Hunter/Apollo egress; app adapters are sidecar clients
**Decision.** Hunter and Apollo keys are read by the sidecar from `.env` (only the keys it
needs); the Python `EnrichmentKitAdapter` (waterfall + people search), `HunterAdapter`
(single-provider enrich + verifier) and `ApolloAdapter` call the sidecar and never send keys.
All keep the paid guard and credential-presence checks; non-loopback sidecar URLs are refused.

## ADR-049 Deliberate deviations from upstream behaviour
**Decision.** Apollo People Match is called with `reveal_personal_emails=false` and
`reveal_phone_number=false` (work email only, no extra credits) and `match_confidence: none`
is a miss; phone numbers are never returned. Verification and domain search are sidecar code
(to keep Hunter's 202/222 semantics and header auth). The upstream cache is disabled.

## ADR-050 Identity from the company's own pages; ambiguity is never enriched
**Decision.** People come from headings / "<Name> is the <Title> of <This company>" sentences on
the verified domain's crawled pages (optionally Hunter Domain Search). Only configured senior
roles; board/advisory/former titles excluded; first+last name required; similar senior people
(same first name + last initial) are AMBIGUOUS and skipped. Provider results naming another
person or domain are rejected; a rejected Hunter early-exit result triggers one budgeted
Apollo follow-up.

## ADR-051 Verification is Hunter's, fresh, and enforced
**Decision.** An email is VERIFIED only with a Hunter `valid` verification inside
`ENRICHMENT_VERIFICATION_TTL_DAYS` (verifier call, or Hunter's own verification carried by the
finder result) and confidence ≥ accept threshold. Apollo's `verified` claim is evidence only.
Expired verification is re-verified or downgraded, never shown as fresh.

## ADR-052 PostgreSQL cache + append-only enrichment history (migration 0006)
**Decision.** `enrichment_runs`, `enrichment_contacts` and `enrichment_attempts` (UPDATE
rejected by trigger; CHECKs: verified ⇒ valid email; enriched ⇒ qualification_id) and a mutable
`enrichment_cache` keyed by (verified domain, normalized person). Monthly budgets are summed
from attempts. The gate additionally compares the decision's ICP snapshot with the current ICP
config (a decision made under another configuration must be re-made).

## ADR-053 Source Actor Registry
**Decision.** `app/domain/sources.py` declares every discovery source with Actor id, policy
state, auth requirement, allowed fields, item/rate/cost limits and parser. A source runs only
if declared + APPROVED + login-free + parser-backed + enabled (`DISCOVERY_SOURCES_ENABLED`);
`DiscoveryService` enforces this before creating a run. Only `funding_news` is approved;
LinkedIn and X are BLOCKED; Reddit, YouTube (Apify), job boards, startup databases and
directories are PENDING_REVIEW. No cookie/credential/CAPTCHA/stealth support exists by design.

## ADR-054 Techno-AI-Router is the single AI provider boundary (Phase 7)
**Decision.** All model calls go through `ReasoningProvider` -> `AiRouterAdapter` ->
`POST /v1/chat/completions` on the Techno-AI-Router (Bearer `tair_` key). No OpenAI/Anthropic/
Google/OpenRouter SDKs or keys; a test fails if `app/` imports a vendor AI SDK or names a vendor
API host. Discovery (`/health`, `/v1/providers`, `/v1/models`) needs no key and is used for the
model policy. 401/403 stop a run; 429/5xx/timeout retry once with backoff.

## ADR-055 Live model catalog + free-price ceiling, never a hard-coded model
**Decision.** Candidate models come from the live `/v1/models` list at runtime: enabled, `llm`,
gateway match and price <= `AI_ROUTER_MAX_PRICE_PER_1M_TOKENS_USD` (default 0 = free only;
unknown price never passes). Primary = configured model if allowed, else the `recommended` one;
fallbacks = configured or next allowed. Sent as router `fallback` and retried client-side when
the router reports the model unavailable. `maxTokens` is camelCase (router contract).

## ADR-056 Versioned service catalog as the only offer source
**Decision.** `app/knowledge/service_catalog.json` (catalog id + version) carries the 9 canonical
TechnoTribes offers from `ai-client-automation/app/knowledge/services.json`, with triggers,
problems, constraints, evidence requirements and hook style. The model may only recommend offer
ids from it; prices, case studies and invented services are excluded. The catalog version is
stored with every analysis.

## ADR-057 Grounded, sectioned output validated before storage
**Decision.** The model must return FACT / INFERENCE / HYPOTHESIS / RECOMMENDATION sections as
strict JSON (unknown fields rejected). Every cited id must exist in the evidence bundle
(`qual_`, `evt_`, `page_`, `contact_`); offers must exist in the catalog; a fact may not rest on
the contact alone; a hypothesis may not restate a fact; the hook may not contain contact data;
confidence below `ANALYSIS_MIN_CONFIDENCE` is rejected. Failures are stored as
`rejected_output` with the reason and a short raw excerpt, never as a recommendation.

## ADR-058 Eligibility re-checked at the service; immutable analyses (migration 0007)
**Decision.** `LeadAnalysisService` re-runs `ensure_enrichment_allowed` and requires a VERIFIED
contact enriched under the same latest decision, whatever the caller passed; otherwise BLOCKED
without a model call. `analysis_runs` + `lead_analyses` are insert-only (UPDATE trigger) with a
CHECK that a `succeeded` row has analysis, offer, qualification and contact. Budgets (per run
count/tokens/USD, monthly USD) are checked before every call.

## ADR-059 The Google Sheet is a read-mostly projection of PostgreSQL (Phase 8)
**Decision.** PostgreSQL stays the system of record. The workbook (`Leads`, `Review_Queue`,
`Export_Runs`) is rebuilt from stored records on every export; edits made in the Sheet never
flow back and the next export overwrites a changed lead row. Human decisions are made in the
dashboard/review service. Bidirectional editing is deferred to a separate, documented change.

## ADR-060 Stable lead id + idempotent upsert by key column
**Decision.** One lead per company, `lead_id = "lead_" + 16 hex of the company UUID`. Upsert
by column A: identical row = no write, different = update in place, absent = insert after the
last used row; a key is never appended twice, duplicates already in the Sheet are reported,
unknown rows are untouched, rows are never deleted (no-longer-qualified leads are marked
`withdrawn`). A foreign header refuses the run. Values are written RAW (no formula
evaluation), cleaned and length-bounded. Row writes per run are capped
(`EXPORT_MAX_ROWS_PER_RUN`).

## ADR-061 Live Sheet writes need a process switch; service account via google-auth
**Decision.** Default is a dry run; `--check` reads only; `--live` additionally needs
`ALLOW_SHEETS_WRITES=true` for that process. Tokens come from the service-account key file
through `google-auth` (now a main dependency); key contents never enter settings, logs or
errors; the spreadsheet id is stored masked (`...last6`) and redacted from logs. The
validation-only exporter is retired. Controlled live tests use `FIXTURE_*` tabs with labelled
synthetic rows and no emails; their history carries no company ids and never counts as exported.

## ADR-062 Human review creates new decisions through domain rules (migration 0008)
**Decision.** approve (REVIEW → QUALIFIED, only if the hard ICP rules pass now under the
current policy + ICP config), reject (REVIEW → NOT_QUALIFIED), reopen (QUALIFIED/NOT_QUALIFIED →
REVIEW). Each applied action inserts a normal `qualification_decisions` row (run
`laya_mode = human`) whose reasons and evidence bundle name the reviewer and reason; existing
decisions are never edited. Every attempt (applied or refused, with `refusal_code`) is stored in
insert-only `human_reviews`. `expected_decision_id` refuses actions on a stale view. Phase 6/7
then admit an approved lead through their own gates; the review path calls no provider.

## ADR-063 Small local dashboard on FastAPI
**Decision.** One static page (`app/web/dashboard.html`, vanilla JS, text-only DOM writes)
plus a JSON API over `DashboardService` / `ReviewService`. Provider health = adapter
`status()` (configuration only, no network, no paid call); usage/cost from stored records,
labelled `provider_reported` vs `ledger_estimate`. The only write endpoint is the review POST,
which requires `X-Dashboard-Request: 1` (cross-site requests cannot add it without a CORS
preflight, and none is allowed). `scripts/run_dashboard.py` binds 127.0.0.1 only.

## ADR-064 Minimal pipeline orchestrator; Phase 9 built inside the final task (migration 0009)
**Context.** The Phase 10 brief assumed Phase 9 existed; the repository audit (2026-10-05)
showed it did not. The user chose to build a minimal Phase 9 first.
**Decision.** `PipelineService` runs the existing stage services in a fixed order with a
company limit, a cost ceiling, per-stage results, a heartbeat and fail-closed behaviour. One
run at a time is enforced by a unique `active_slot` column on `pipeline_runs`. Candidate
queries select only companies that need a stage, and never re-decide a human review decision.
Dry run is the default; a writing run needs live provider mode. Stale runs are closed by
recovery. A small interval scheduler process triggers runs. No queue, no workers, no alerting.

## ADR-065 OpenAI (Responses API) replaces the Techno-AI-Router as the reasoning provider
**Decision.** `OpenAIAdapter` implements the unchanged `ReasoningProvider` interface with the
official SDK (`AsyncOpenAI.responses.create`, `store=False`, no tools). `REASONING_PROVIDER`
selects the adapter (default `openai`); the router adapter is deprecated, registered only on
explicit opt-in and its key is no longer required. `ProviderName.OPENAI` is new; historical
records keep their original provider values. The model is configuration (`OPENAI_MODEL`),
validated with a free `models.retrieve` before the first generation.

## ADR-066 Domain schema stays authoritative; provider gets an adapted strict schema
**Decision.** `ChatRequest.response_schema` carries the Pydantic JSON Schema of
`LeadAnalysisOutput`. The OpenAI adapter converts it to strict Structured Outputs format (all
properties required, `additionalProperties: false`, value constraints moved to descriptions).
The answer is still validated by the domain (schema, evidence ids, catalog, grounding).
Refusal, incomplete and malformed answers are typed errors; billed-but-unusable calls are
stored as FAILED analyses with their usage and cost.

## ADR-067 AI spend is estimated and budgeted at four levels
**Decision.** Cost = reported tokens x price (built-in table checked against the pricing page,
overridable; unknown model without a price is refused). Budgets: worst case of a single call
(before the call), per analysis run, per month (`OPENAI_MAX_ESTIMATED_COST_USD_*`), per
pipeline run. Paid guard order: paid switch, key, price, budget, model check.

## ADR-068 Compose is the deployment unit; private services; loopback publishing
**Decision.** One `docker-compose.yml` starts PostgreSQL, Crawl4AI, Laya, enrichment-kit, a
forward-only `migrate` step, the app and the scheduler. Images build from `uv.lock`, run
non-root, read-only, without capabilities. Only loopback ports are published (the earlier
`0.0.0.0:5432` binding is removed; IPv6 loopback is published too because `localhost` resolves
there first). Sidecar endpoints must be loopback or listed in `INTERNAL_SERVICE_HOSTS`; the
enrichment sidecar refuses a non-loopback bind without its token. `DATABASE_HOST_OVERRIDE` /
`DATABASE_NAME_OVERRIDE` adjust only the host / database of `DATABASE_URL`. The data volume is
external so Compose can never delete it. Live writing runs execute as one-off containers with
process-scoped switches.

## ADR-069 One operator token; explicit CORS; bounded requests
**Decision.** All `/api/v1` routes require `OPERATOR_API_TOKEN` when set; staging/production
refuse to start without one (24+ characters); without a token only loopback clients are
served. Writes also need `X-Dashboard-Request`. CORS is off unless exact origins are listed.
Bodies are limited (64 KB). Security headers on every response. No RBAC: the tool has one
operator role; multi-user access belongs to a reverse proxy.

## ADR-070 Logical backups proven by restore; no automatic reset
**Decision.** `scripts/backup_db.py` (pg_dump custom format inside the container, timestamped
file, retention) and `scripts/restore_db.py` (restore into a `lead_intel_restore*` scratch
database, compare version and row counts with live). A backup is accepted only after a restore
test. Migrations are forward-only and explicit; nothing resets the database.

## ADR-071 Synthetic data is isolated and labelled
**Decision.** Demo data lives in memory or in a database named `lead_intel_demo...` (the
seeder refuses any other name). Exports from it are forced to `FIXTURE_` tabs with labelled
names and no emails. The dashboard header names the database in use. The ICP is never relaxed
and decisions are never forged for a demo or a test.

## ADR-072 An explicit company limit is honoured or refused, never reduced
**Context.** `--limit 10` was silently cut to `PIPELINE_COMPANY_LIMIT` (5), so an operator
believed ten companies had been considered.
**Decision.** `PIPELINE_COMPANY_LIMIT` is only the default. An explicit limit is used as given
up to `PIPELINE_MAX_COMPANY_LIMIT` (default 25, max 50) and rejected with a configuration error
beyond it. Stage budgets (Apify, Firecrawl, Hunter/Apollo, OpenAI, pipeline cost) still apply.

## ADR-073 Discovery is multi-query and multi-source inside the existing Apify budgets
**Decision.** The pipeline's discovery stage builds one request per enabled runnable source
from a configurable ICP query set (`DISCOVERY_QUERIES`, `DISCOVERY_NEWS_QUERIES`), sends as
many queries as `APIFY_MAX_ITEMS_PER_RUN` allows (one results page each), rotates through the
set by the number of earlier runs of that source, and limits results to the 90-day trigger
window (`DISCOVERY_QUICK_DATE_RANGE`). A second registry source `startup_news` (launch /
expansion / hiring headlines, same public Google Search Actor, same pinned-safe input) is
APPROVED but off until listed in `DISCOVERY_SOURCES_ENABLED`. One source failing does not hide
the others. Discovery stays opt-in (`--stages all`) because it spends credit. Login-gated
sources remain blocked in the registry and cannot be enabled by configuration.

## ADR-074 Phone is an optional capability with its own switch and budget
**Decision.** `ENRICHMENT_PHONE_ENABLED` (default false) and
`ENRICHMENT_MAX_PHONE_LOOKUPS_PER_RUN`. When on, the sidecar passes through the number Hunter
already returns with an Email-Finder result (no extra credit); Apollo's paid, asynchronous
mobile reveal is never requested. A number is stored only from an identity-matched result,
with `phone_status` (`not_requested | found | not_found | budget_exhausted`) and provenance;
a database CHECK ties a stored phone to status `found`. Migration 0010 is additive.
**Consequence.** Most contacts have no phone; that is recorded, not hidden.

## ADR-075 LinkedIn URLs are metadata, never fetched
**Decision.** A founder URL comes from a provider result for the identity-matched person or
from a `linkedin.com/in/...` link the company's own crawled page shows next to that person's
name; a company URL comes from the website crawl's social links. Each is stored with its
source. No request is ever made to LinkedIn, no login or cookie is used.

## ADR-076 Lead profile is derived at read time; Leads layout v2 converts the old tab
**Decision.** Location, industry, company LinkedIn and funding are computed in the lead view
from records already stored (company row, geography hard rule, homepage title/description,
social links, the decision's funding event) - no new provider call, no new table, and an empty
value when the evidence has none (`field_provenance` says `NOT_FOUND`). The `Leads` header was
reordered for operators; column A stays `lead_id`. A tab with the previous header is converted
in place: every existing row is re-laid-out by column name, so nothing is dropped and nothing
sits under a wrong header. Any other header is still refused. Decisions are never read back
from the Sheet.
