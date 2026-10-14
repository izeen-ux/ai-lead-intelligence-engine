# Enrichment sidecar (Phase 6)

Local HTTP service (127.0.0.1 only) around a **pinned, locally built** copy of
[enrichment-kit](https://github.com/masteranime/enrichment-kit) (`6998898c03`, tag v0.1.1,
package 0.1.0, MIT). It is the only process that talks to Hunter and Apollo.

Why a project-owned sidecar: enrichment-kit is a TypeScript library + CLI with no HTTP mode and
is not published on npm (verified 2026-10-01). Upstream sources are not modified; the build is
library-only because upstream `npm run build` fails type-checking `src/cli` (TS18047).

## Setup (once) and run
```powershell
cd sidecars\enrichment-kit
node setup.mjs          # clones the pinned commit into vendor/, npm install --ignore-scripts, builds
npm test                # 14 offline tests (fetch stubbed; real upstream Waterfall)
node server.mjs         # http://127.0.0.1:8787 (keep the window open)
```
It reads ONLY `HUNTER_API_KEY`, `APOLLO_API_KEY`, `ENRICHMENT_KIT_API_TOKEN`,
`ENRICHMENT_KIT_PORT`, `ENRICHMENT_KIT_MAX_PROVIDER_CALLS`, `ENRICHMENT_KIT_TIMEOUT_MS` from the
project `.env` (process env wins). Set `ENRICHMENT_KIT_API_TOKEN` (any long random string) in
`.env`; the app sends it as a bearer token.

## Routes
| Route | Does | Credits |
|---|---|---|
| `GET /health` | config + counters (no provider call) | 0 |
| `POST /v1/enrich` | upstream Waterfall: Hunter Email Finder → Apollo People Match (work email only) | Hunter 1 if found; Apollo on match |
| `POST /v1/verify` | Hunter Email Verifier (keeps 202 pending / 222 semantics) | 1 |
| `POST /v1/people` | Hunter Domain Search (executives, personal emails, ≤10) | 1 if results |
| `POST /v1/account` | Hunter account usage counters (free; personal data dropped) | 0 |

Deliberate deviations from upstream defaults: Apollo `reveal_personal_emails=false`,
`reveal_phone_number=false`, `match_confidence: none` = miss; upstream cache disabled (the app
caches in PostgreSQL); a per-process provider-call cap; typed error kinds (auth, rate_limited,
quota_exhausted, pii_blocked, timeout, bad_request, provider_error, budget_exhausted).
Logs: method, path, status, duration only.
