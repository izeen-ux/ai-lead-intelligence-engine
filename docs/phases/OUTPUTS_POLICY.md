# Outputs Policy (Phase 8) — Google Sheets, Dashboard, Human Review

**PostgreSQL is the system of record.** The Google Sheet and the dashboard are projections of
it. Nothing typed into the Sheet ever flows back into the database; human decisions are made in
the dashboard / review service, which writes new immutable records through the domain rules.

## 1. What a "lead" is

One lead per company. `lead_id = "lead_" + first 16 hex characters of the company UUID`
(e.g. `lead_592fdb05566c4b4b`). It never changes when the company is re-qualified,
re-enriched or re-analysed, so every export updates the same Sheet row.

A lead view is assembled from stored records only (`app/services/leads.py`):

| Part | Rule |
|---|---|
| decision | the company's **latest** qualification decision |
| contact | newest contact enriched **under that decision**: VERIFIED first, otherwise the newest named person (shown with its status) |
| analysis | newest **SUCCEEDED** analysis for that decision **and** that contact |

`export_status`: `ready` (verified contact + accepted analysis) · `awaiting_contact` ·
`awaiting_analysis` · `withdrawn` (was exported, latest decision is no longer QUALIFIED).

## 2. Workbook schema

Tab names are configurable (`GOOGLE_SHEETS_WORKSHEET`, `GOOGLE_SHEETS_REVIEW_WORKSHEET`,
`GOOGLE_SHEETS_RUNS_WORKSHEET`); defaults below. Row 1 is the header; column A is the key.

### Leads — companies whose latest decision is QUALIFIED (layout v2, 2026-10-05)

Column A is always `lead_id`. The first 32 columns are the operator-facing lead sheet; the last
6 keep the detail / traceability columns of the Phase 8 layout.

| Column | Source (PostgreSQL) | May be blank? |
|---|---|---|
| lead_id | derived from `companies.id` | no |
| company_name | `companies.name` | no |
| website | `companies.domain` when `domain_status = verified` | no (QUALIFIED needs it) |
| company_location | `companies.location` / `country_code`; else `India` when the ICP geography hard rule passed (its reason is the provenance) | rarely |
| industry | `companies.industry`; else a whole-word keyword match on the company's **own** homepage title / description (the matched term is the provenance) | yes |
| founder_name / founder_title | chosen contact: `enrichment_contacts.full_name / title` | until enriched |
| founder_email | `enrichment_contacts.email` **only when the contact is VERIFIED** | yes |
| email_status | `enrichment_contacts.email_verification` | until enriched |
| founder_phone | `enrichment_contacts.phone`, only for a VERIFIED contact | **yes - normal** |
| phone_status | `not_requested` (capability off, the default) · `found` · `not_found` · `budget_exhausted` | until enriched |
| founder_linkedin_url | `enrichment_contacts.linkedin_url` (provider result, or a profile link on the company's own page) | **yes - normal** |
| company_linkedin_url | a `linkedin.com/company/...` link in `website_crawls.social_links` | **yes - normal** |
| funding_amount / funding_date | the attributed funding event of the decision's evidence bundle (`round_amount`, `date`) | yes (intent path) |
| funding_round | round named in that event's headline, else in the discovery headline | yes |
| qualification_decision / qualification_path | `qualification_decisions.decision / path` | no |
| qualification_confidence | `qualification_decisions.confidence` | yes |
| primary_trigger | `lead_analyses.primary_trigger` | until analysed |
| verified_contact | `yes` when the chosen contact is VERIFIED, else `no` | no |
| ai_opportunity | first `analysis.ai_opportunities` item (with evidence ids) | until analysed |
| recommended_offer | catalog name + `(recommended_offer_id)` | until analysed |
| offer_match_reason / outreach_hook | `analysis.offer_match_reason / outreach_hook` | until analysed |
| strongest_signal_date | date of the first event in the evidence bundle (qualifying events first) | rarely |
| evidence_sources | up to 5 source URLs of those events | rarely |
| qualification_id / contact_id / analysis_id | the record ids the row was built from | contact / analysis until they exist |
| last_updated_at | max(decision, contact, analysis) timestamp (UTC) | no |
| export_status | see §1 | no |
| company_id | `companies.id` | no |
| observed_facts / pain_hypotheses | `lead_analyses.analysis` (≤3 each, with evidence ids) | until analysed |
| analysis_confidence | `lead_analyses.confidence` | until analysed |
| evidence_count | `qualification_decisions.event_count` (deduplicated events) | no |
| field_provenance | one line per field: where the value came from, or `NOT_FOUND` | no |

**Never fabricated.** A value that the stored evidence does not contain stays empty and its
`field_provenance` line says `NOT_FOUND` (or the phone status). A blank phone, LinkedIn URL,
industry or funding cell is a legitimate result, not an error:

```
founder_name: https://khetilabs.in/about            <- page the person was found on
founder_title: website
founder_email: hunter (confidence 0.92)
founder_phone: not_requested | not_found | budget_exhausted | <provider>
founder_linkedin_url: hunter | apollo | website: <page url> | NOT_FOUND
company_location: qualification geography rule: inferred IN from ccTLD of ...
industry: keyword 'farmers' on the company's homepage | company record | NOT_FOUND
company_linkedin_url: link on the company website | NOT_FOUND
funding: funding event evt_... (publisher) | NOT_FOUND
```

LinkedIn pages are never fetched: founder URLs come from a provider result or from a link the
company's own website shows next to the person's name; company URLs come from the website's
link metadata. No login, no cookies.

A `withdrawn` row keeps identity and reason but blanks `founder_email`, `founder_phone`,
`outreach_hook`, `recommended_offer`, `ai_opportunity`. Rows are never deleted.

**Layout v1 → v2.** A `Leads` tab that still has the Phase 8 header (26 columns starting
`lead_id, company_id, company_name, verified_domain, ...`) is converted in place by the next
live export: row 1 is rewritten and **every** existing row is moved to the new positions by
column name (`verified_domain → website`, `contact_name → founder_name`,
`contact_title → founder_title`, `work_email → founder_email`). Rows the run would not have
rewritten (old leads, manual notes, rows beyond the write budget) are moved too, so no row is
left under the wrong header and none is dropped. Any other header is still refused.

### Review_Queue — companies whose latest decision is REVIEW

`lead_id, company_id, company_name, verified_domain, queue_status (pending | resolved:
<decision>), qualification_decision, qualification_path, review_reasons, hard_rules,
signals (≤3 events: date, headline, publisher count), evidence_sources, contact_state,
analysis_state, qualification_id, decided_at`.

### Export_Runs — one audit row per live run (never updated)

`run_id, started_at, finished_at, mode, status, leads_considered, inserted, updated, unchanged,
withdrawn, skipped, failed, duplicate_rows_found, worksheets, error_summary`.

## 3. Sync rules (idempotent upsert)

1. Read the tab. Row 1 must equal the header, be empty, be a **prefix** of it (columns
   appended), or - for `Leads` - be the Phase 8 layout, which is converted in place (§2).
   Anything else → `sheet_header_mismatch`, nothing written (someone else's tab).
2. Index column A. A key present → compare the full row: identical = `unchanged` (no write),
   different = `update` **in place**. Key absent → `insert` after the last used row.
3. A key is never appended twice. Keys found more than once in the Sheet are reported
   (`duplicate_rows_found`); only the first occurrence is written.
4. Rows whose key the app does not know (notes, other data) are never touched.
5. `EXPORT_MAX_ROWS_PER_RUN` (default 50) caps row writes per run; the rest are `skip` and
   catch up on the next run. `--limit` caps companies read per tab.
6. Values are written with `valueInputOption=RAW` (a leading `=` is text, never a formula);
   control characters are removed; cells are cut at 2,000 characters.
7. One `values:batchUpdate` per tab (≤100 rows per request); 429 / 5xx / timeout retried up to
   `GOOGLE_SHEETS_MAX_RETRIES` with backoff (2 s, 4 s, … ≤16 s); 401/403/404 stop the run.

## 4. Write authorization

| Mode | What happens |
|---|---|
| default (dry run) | plan only; demo data unless `--db`; no Google call |
| `--check` | reads the real Sheet (credentials, workbook, tabs, rows) and plans; writes nothing |
| `--live` | writes; needs `--db` or `--fixture`, **`ALLOW_SHEETS_WRITES=true` for that process**, `GOOGLE_SHEETS_SPREADSHEET_ID` and the key file; history always stored |
| `--fixture` / database `lead_intel_demo...` | synthetic demo leads to `FIXTURE_*` tabs only; company name, founder name, phone and LinkedIn URL prefixed `[FIXTURE - synthetic]`, **no email**; never the official tabs |

The qualification gate is never bypassed to fill the Sheet: only QUALIFIED companies reach
the Leads tab.

## 5. History (migration 0008)

`export_runs` (mode, masked spreadsheet ref `...last6`, counts, api calls, error) and
`export_items` (one row per planned row: worksheet, lead_id, company / qualification /
contact / analysis ids, action, row number, sha256 of the cells, status, error) —
**insert-only** (UPDATE trigger). The full spreadsheet id is never stored or logged.

## 6. Human review

| Action | Allowed from | Creates |
|---|---|---|
| approve | REVIEW, **and** hard ICP rules pass now (current policy + ICP config) | new QUALIFIED decision |
| reject | REVIEW | new NOT_QUALIFIED decision |
| reopen | QUALIFIED or NOT_QUALIFIED | new REVIEW decision (QUALIFIED → loses enrichment/analysis eligibility at once) |

- Hard rules are re-evaluated from the stored signals at the moment of the action; Laya and
  the LLM reviewer are not called. A human can resolve a REVIEW (e.g. over-cap funding on an
  intent-path lead) but can never qualify a company whose hard rules fail.
- The new decision is a normal `qualification_decisions` row (run with `laya_mode = human`),
  its reasons start with `human <action> by <reviewer>: <reason>` and the evidence bundle
  carries `human_review {action, reviewer, reason, prior_decision_id}`.
- Every attempt is audited in `human_reviews` (insert-only): reviewer, reason, time, prior and
  resulting decision, hard-rule results, outcome `applied | refused` with `refusal_code`
  (`hard_rules_failed`, `stale_decision`, `not_allowed_from_<state>`).
- `expected_decision_id` (sent by the dashboard) refuses an action taken on an outdated view.
- After approval, Phase 6 / 7 admit the lead through their own gates when they are next run.
  The review service and dashboard never call Hunter, Apollo, the AI provider or any other provider.

## 7. Dashboard

`uv run python scripts/run_dashboard.py` → `http://127.0.0.1:8000/dashboard` (127.0.0.1 only;
`--demo` for synthetic data; in the demo and on a `lead_intel_demo` database every company and contact value is prefixed `[FIXTURE - synthetic]`). Sections: pipeline counts, provider health (adapter `status()` —
configuration only, **no network call**), usage/cost from stored records labelled
`provider_reported` or `ledger_estimate`, review queue, leads (filter by decision, pagination),
lead detail (trace ids, hard rules, events with sources, contact incl. phone status and LinkedIn, company profile + field provenance, analysis + offer reason,
export state, decision history, review audit) and recent exports. The only write is
`POST /api/v1/review/{company_id}`, which requires the `X-Dashboard-Request: 1` header.
