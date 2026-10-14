# Lead Analysis Policy (Phase 7)

Policy version **`p7-2026.10.01`** · prompt **`lead-analysis-v1`** · catalog
**`technotribes-services` `2026-10-01.1`**. All three are stored on every analysis record.

## 1. Who gets analysed

A company is analysed only if, **at the service boundary** (re-checked every time, never trusted
from the caller or the candidate query):

1. `ensure_enrichment_allowed` passes — latest qualification decision is **QUALIFIED**, made
   under the current qualification policy and ICP configuration; **and**
2. a Phase 6 contact with status **VERIFIED** exists that was enriched under **that same
   decision** (`qualification_id` match) and has a full name.

Otherwise a **BLOCKED** record is written (`enrichment_not_allowed` or `no_usable_contact`) and
the model is never called. REVIEW and NOT_QUALIFIED leads are never analysed.
`ANALYSIS_REQUIRE_VERIFIED_CONTACT=false` additionally admits UNVERIFIED contacts (off by default).

## 2. What the model sees (evidence bundle)

Compact, deduplicated, every item with a stable id the model must cite:

| Item | Id | Content |
|---|---|---|
| qualification | `qual_<12hex>` | decision, path, top 5 reasons |
| events | `evt_<12hex>` | Phase 4 signals **clustered** (same event from several publishers = one event), date, types, headline, publisher count, ≤3 source URLs; max `max_events` (8) |
| pages | `page_<12hex>` | ≤3 excerpts of the company's own crawled pages (home/about/team first), markdown stripped, ≤1200 chars |
| contact | `contact_<12hex>` | name, title, role — **no email/phone** |

Plus the full service catalog (offer ids, descriptions, triggers, problems, constraints,
evidence requirements, hook style). No raw database rows, no secrets.

## 3. Output contract (strict)

JSON object, unknown fields rejected:

| Kind | Field | Rule |
|---|---|---|
| FACT | `observed_facts[]` `{statement, evidence_ids}` | 1–8; stated in the evidence; must cite; may not cite only the contact |
| INFERENCE | `evidence_based_inferences[]` | reasoned from cited facts |
| HYPOTHESIS | `pain_hypotheses[]` `{statement, confidence, evidence_ids}` | 1–5; may not restate a fact |
| RECOMMENDATION | `ai_opportunities[]` `{description, offer_id?, evidence_ids}` | offer ids must exist in the catalog |
| | `primary_trigger` | funding · ai_automation · expansion · customer_deployment · hiring_growth · technology · other |
| | `recommended_offer_id` | must be one of the 9 catalog offers |
| | `offer_match_reason`, `outreach_hook` | hook may not contain an email/phone |
| | `confidence` 0–1, `supporting_evidence_ids[]` | every cited id must be in the bundle |

Stored alongside: model, gateway, upstream provider, usage (prompt/completion/total tokens),
latency, attempts, cost, policy/prompt/catalog versions, timestamps.

## 4. Rejections (status `rejected_output`, raw excerpt kept ≤500 chars)

`malformed_json` · `schema_violation` (missing/extra fields, ranges) · `unsupported_evidence`
(id not in bundle) · `invalid_offer` (offer not in catalog) · `grounding_violation` (contact-only
fact, hypothesis-as-fact, contact data in hook) · `low_confidence` (< `ANALYSIS_MIN_CONFIDENCE`,
default 0 = off). Rejected analyses are never shown as recommendations.

## 5. Model policy (OpenAI)

- Provider: OpenAI **Responses API** behind the `ReasoningProvider` capability
  (`REASONING_PROVIDER=openai`, the default). Model = `OPENAI_MODEL` (default `gpt-6-luna`);
  the model is checked against the account's live model list before a writing run.
- Output is requested with a **strict JSON schema** (`text.format = json_schema, strict`), so a
  response either matches the analysis schema or is rejected - never repaired or guessed.
  `store=false`: OpenAI does not retain the request.
- Budgets, checked before every call: `OPENAI_MAX_TOKENS` (per response),
  `OPENAI_MAX_TOKENS_PER_RUN`, `OPENAI_MAX_ESTIMATED_COST_USD_PER_RUN`,
  `OPENAI_MAX_ESTIMATED_COST_USD_PER_MONTH`, plus the `ANALYSIS_*` budgets above.
- Typed errors: missing / rejected key stops the whole run (`provider_not_configured`); a
  refusal (`provider_refused`), a truncated answer (`provider_incomplete_response`) or a budget
  stop (`provider_budget_exceeded`) gives a FAILED / BLOCKED record and the run continues.
- Usage (input / output tokens) and the estimated cost of every call are stored with the
  analysis record.
- The earlier Techno-AI-Router adapter is deprecated and only used when an operator sets
  `REASONING_PROVIDER=ai_router`; its history is in
  [PHASE_7_COMPLETION.md](PHASE_7_COMPLETION.md).

## 6. Budgets (checked before every model call)

| Setting | Default | Effect |
|---|---|---|
| `ANALYSIS_MAX_PER_RUN` | 5 | analyses per run (BLOCKED records don't count) |
| `ANALYSIS_MAX_TOKENS_PER_RUN` | 30000 | tokens per run |
| `ANALYSIS_MAX_COST_USD_PER_RUN` | 0 | USD per run (free models cost 0) |
| `ANALYSIS_MAX_COST_USD_PER_MONTH` | 0 | USD month-to-date (from stored analyses) |

On exhaustion the run stops with `error_code=budget_exhausted`; already-written records stay.

## 7. History

`analysis_runs` + `lead_analyses` (migration 0007). Analyses are **insert-only** — a database
trigger rejects UPDATE; a CHECK forbids a `succeeded` row without analysis, offer, qualification
and contact. Re-analysis adds a new record.
