# Enrichment Policy & Configuration (Phase 6)

**Policy version:** `p6-2026.10.01` (`ENRICHMENT_POLICY_VERSION`, `app/domain/enrichment/config.py`;
stored on every run and contact). **Scope:** QUALIFIED companies only.

## 1. Pipeline (fixed order) — `app/services/enrichment.py`

| # | Stage | Rule | Paid? |
|---|---|---|---|
| 1 | Gate | `ensure_enrichment_allowed(store, company, icp)`: latest decision QUALIFIED, hard rules passed, current qualification `POLICY_VERSION` **and** current ICP configuration. Refused → `blocked` record, zero provider calls | no |
| 2 | Identity | decision-makers from the company's own crawled pages; optional Hunter Domain Search | search: yes (1 credit if results) |
| 3 | Cache | fresh result + fresh verification → reused (no call); fresh result + stale verification → re-verify only | no / verify only |
| 4 | Budget | per-lead, per-run, per-month, sidecar-call and USD limits checked **before** every paid call | — |
| 5 | Waterfall | enrichment-kit (sidecar): Hunter Email Finder → Apollo People Match (early exit ≥ 0.85) | yes |
| 6 | Merge | person/domain matching, Hunter-first merge, per-field provenance; if Hunter's early-exit result is rejected, one budgeted Apollo follow-up | — |
| 7 | Verify | Hunter Email Verifier unless Hunter's finder result carries a fresh `valid` verification | yes (1) |
| 8 | Persist | append-only attempts + contacts, cache upsert, run totals | — |

## 2. Identity rules — `app/domain/enrichment/identity.py`

* **Sources (never invented):** the company's OWN pages on the verified domain
  (`website_pages`): markdown headings `### <Name> - <Title>` and sentences
  `<Name> is the <Title> of|at <Company>` **where <Company> is this company**. A bio saying
  "Co-Founder at <other company>" is ignored. Optional `ENRICHMENT_PEOPLE_SEARCH=hunter`:
  Hunter Domain Search (executives) on the verified domain when the pages name nobody.
* **Eligible roles:** `ENRICHMENT_ROLE_PRIORITY` (default founder, co-founder, ceo, chief
  executive, managing director, cto, owner, coo, president). Never: independent/non-executive,
  board member, advisor, investor, former, mentor, intern.
* **Ranking:** best role rank, then number of matched senior roles ("CEO and Co-Founder" beats
  "Co-Founder"), then number of independent sources, then name.
* **Name:** first + last name required (no single names, no initials-only).
* **Ambiguity:** two different senior people with the same first name and last initial →
  both `ambiguous`, no provider call.
* **Multiple founders:** up to `ENRICHMENT_MAX_PERSONS_PER_COMPANY` (default 1); others recorded
  as skipped with the reason.
* **Result matching:** provider result rejected when it names a different person
  (`person_mismatch`), reports another company domain (`company_domain_mismatch`), returns an
  email off the verified domain (`email_domain_mismatch`), or is below
  `ENRICHMENT_MIN_CONFIDENCE`.

## 3. Merge & verification — `app/domain/enrichment/policy.py`

* Highest-confidence accepted email wins; **ties go to Hunter**; Apollo never overwrites a
  higher-confidence Hunter email. Title / LinkedIn URL may be complemented; every field records
  its provider (`provenance.email|title|linkedin_url`), plus identity sources, rejections and
  the waterfall summary.
* **A discovered email is UNVERIFIED until Hunter verification says `valid`.** Counts as
  verification: Hunter Email Verifier, or Hunter's own `verification.status=valid` on the finder
  result dated within `ENRICHMENT_VERIFICATION_TTL_DAYS`. Apollo's `email_status=verified` is
  kept as evidence only.
* **Final status:** `verified` (valid + confidence ≥ `ENRICHMENT_ACCEPT_CONFIDENCE`) ·
  `unverified` (accept_all / unknown / webmail / pending / verification error or skipped /
  low confidence) · `invalid_email` (invalid / disposable) · `not_found` · `no_person` ·
  `ambiguous` · `budget_exhausted` · `blocked` · `failed`. DB CHECK: `verified` ⇒ email present
  and verification `valid`; any enriched status ⇒ `qualification_id` present.

## 3a. Optional fields: phone and LinkedIn (final lead-output expansion, 2026-10-05)

Both are **optional**. A blank value with a status is a normal, correct result.

**Phone - OFF by default.**

| Setting | Default | Meaning |
|---|---|---|
| `ENRICHMENT_PHONE_ENABLED` | `false` | master switch of the phone capability |
| `ENRICHMENT_MAX_PHONE_LOOKUPS_PER_RUN` | `5` | its own budget: contacts per run that may be asked for a phone |

* Off → the request says `includePhone: false`, the sidecar strips any number and the app drops
  one even if it arrived. Contact `phone_status = not_requested`.
* On → the email-finder request carries `includePhone: true`. The only source is the number
  Hunter already returns with an Email-Finder result (**no extra call, no extra credit**).
  **Apollo's mobile reveal (8 credits, asynchronous webhook) is never requested** - it stays
  pinned `reveal_phone_number=false`; enabling it would be a separate, explicitly budgeted
  feature.
* A number is kept only when it comes from a result that passed the identity checks (same
  person, same verified domain) and looks like a phone number (7-15 digits).
* `phone_status`: `not_requested` · `found` · `not_found` · `budget_exhausted`
  (DB CHECK: a phone is stored ⇔ status `found`). Provenance: `provenance.phone =
  {status, provider}`.
* Cached results keep their phone state for the cache TTL: switching the capability on does
  not re-buy contacts enriched while it was off.

**Founder LinkedIn URL - never fetched, never guessed.** In order:
1. the URL a provider returned for the identity-matched person (`provenance.linkedin_url =
   hunter | apollo`);
2. else a `linkedin.com/in/...` link that the company's **own** crawled page shows within 400
   characters after that person's name and before the next heading
   (`provenance.linkedin_url = "website: <page url>"`);
3. else empty.

The LinkedIn site itself is never requested (no login, no cookies, no scraping): only link
metadata the pipeline already holds is used. The company LinkedIn URL of the lead view comes
from the website crawl's social links (see OUTPUTS_POLICY §2).

## 4. Cache

Key = sha256(`<verified domain>|<normalized person name>`)[:40] (`enrichment_cache`). Stores
provider, email, verification state + `verified_at`, confidence, title, LinkedIn URL, phone +
phone status, provenance, `cached_at`, `expires_at` (`ENRICHMENT_CACHE_TTL_DAYS`, default 30), hit count.
Negative results (no email) are cached too, so known misses are not re-bought within the TTL.
An expired verification is never shown as fresh: it is re-verified (budget permitting) or the
contact is downgraded to `unverified`. The upstream enrichment-kit cache is disabled (the app
owns caching in PostgreSQL).

## 5. Budgets (all `.env`, checked before every paid call)

| Setting | Default | Scope |
|---|---|---|
| `ENRICHMENT_MAX_ATTEMPTS_PER_LEAD` | 4 | provider operations per person per run |
| `ENRICHMENT_MAX_SIDECAR_CALLS_PER_RUN` | 10 | sidecar requests (enrich / verify / people) |
| `ENRICHMENT_MAX_HUNTER_CREDITS_PER_RUN` / `_PER_MONTH` | 5 / 50 | Hunter credits |
| `ENRICHMENT_MAX_APOLLO_CREDITS_PER_RUN` / `_PER_MONTH` | 3 / 20 | Apollo credits |
| `ENRICHMENT_MAX_COST_USD_PER_RUN` / `_PER_MONTH` | 0.50 / 5.00 | USD estimate (`*_CREDIT_USD`) |
| `ENRICHMENT_KIT_MAX_PROVIDER_CALLS` | 50 | **sidecar process** hard cap (defence in depth) |
| `ENRICHMENT_MAX_PHONE_LOOKUPS_PER_RUN` | 5 | contacts asked for a phone (only when `ENRICHMENT_PHONE_ENABLED=true`) |

Monthly usage = sum of `enrichment_attempts.credits` since the 1st of the month (UTC). Worst-case
credit per operation is assumed before a call (finder 1, match 1, verify 1, domain search 1);
actual credits are recorded after (finder 0 when nothing found; Apollo 0 on no match).
When no paid vendor fits any budget the run stops (`error_code=budget_exhausted`); a person
whose waterfall no longer fits gets `budget_exhausted`; a verification that no longer fits is
`skipped` and the contact stays `unverified`. **Live observation (2026-10-01):** one Hunter
Email-Finder call moved Hunter's own counters by 1 search AND 2 verifications - Hunter's
account counters (`scripts/run_enrichment.py --live` prints before/after) are the source of
truth for spend; the ledger counts one credit per operation.

## 6. Thresholds

`ENRICHMENT_MIN_CONFIDENCE` 0.5 (accept a provider result) · `ENRICHMENT_EARLY_EXIT_CONFIDENCE`
0.85 (stop the waterfall) · `ENRICHMENT_ACCEPT_CONFIDENCE` 0.7 (a `valid` email below this stays
`unverified`) · min ≤ early-exit enforced by settings validation.

## 7. Safety invariants

* No NOT_QUALIFIED / REVIEW / never-qualified / stale-policy / other-ICP company reaches a
  provider (service gate + CHECK constraint + tests; AST test: only the gated service and wiring
  import the vendor adapters).
* Paid calls need `PROVIDER_MODE=live` **and** `ALLOW_PAID_PROVIDER_CALLS=true` (adapter guard).
* Hunter/Apollo keys are read only by the sidecar; the app only checks presence and never sends
  them; the sidecar never logs bodies, drops personal emails, and drops phone numbers unless
  the request explicitly asks for one (`includePhone`, see §3a).
* History (`enrichment_contacts`, `enrichment_attempts`) is append-only (UPDATE trigger).
