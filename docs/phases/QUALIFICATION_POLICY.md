# Qualification Policy & Configuration (Phase 5)

**Policy version:** `p5-2026.09.30` (constant `POLICY_VERSION` in `app/domain/qualification/icp.py`;
stored on every run and decision; bump it whenever rules, clustering or thresholds change
meaning).
**Outputs:** `QUALIFIED` · `REVIEW` · `NOT_QUALIFIED` (`QualificationDecision`).

Principle: **hard business rules are never overridden by Laya or an LLM.** Semantic stages can
make an outcome *more cautious* freely; only cases the hard rules permit can become QUALIFIED.

## 1. Pipeline order (fixed)

| # | Stage | Code | Deterministic? |
|---|---|---|---|
| 1 | Identity / domain eligibility | `rules.check_identity` | yes |
| 2 | Hard ICP rules (geography, funding path, intent path) | `rules.evaluate_hard_rules` | yes |
| 3 | Signal normalization + event clustering | `clustering.cluster_signals` | yes |
| 4 | Evidence bundle | `bundle.build_bundle` | yes |
| 5 | Laya typed decisions (only if a path passed) | `LayaAdapter` / `MockLayaProvider` | model |
| 6 | Optional LLM review (REVIEW only, off by default) | `LlmReviewProvider` | model |
| 7 | Final decision | `policy.decide` / `policy.apply_llm_review` | yes |
| 8 | Persist immutable decision + evidence ids | `SqlQualificationStore.insert_decision` | — |
| 9 | Phase 6 door | `services.enrichment_gate.ensure_enrichment_allowed` | yes |

(Clustering runs before the rules evaluate funding/intent, because the rules operate on events.)

## 2. Configuration (all in `.env`; read only through `IcpConfig`)

| Setting | Default | Meaning |
|---|---|---|
| `ICP_COUNTRY_CODE` | `IN` | ICP geography |
| `ICP_COUNTRY_TLDS` | `in` | ccTLDs accepted as geography evidence (e.g. `drivn.co.in`) |
| `FUNDING_CAP_INR` | `50000000` | max qualifying ROUND amount (₹5 Cr), inclusive |
| `ICP_USD_TO_INR_RATE` | *(empty)* | FX for USD rounds; empty = USD amounts are unknown |
| `ICP_FUNDING_WINDOW_DAYS` | `90` | funding event must be ≤ 90 days old (inclusive) |
| `ICP_INTENT_WINDOW_DAYS` | `90` | intent event must be ≤ 90 days old (inclusive) |
| `ICP_OVER_CAP_FUNDING_POLICY` | `review` | intent-qualified company with a recent round above the cap: `ignore` / `review` / `reject` |
| `ICP_MIN_SIGNAL_CONFIDENCE` | `0.6` | minimum Phase 4 confidence of an intent event |
| `CLUSTER_TITLE_SIMILARITY` | `0.5` | headline Jaccard for "same event" |
| `CLUSTER_WINDOW_DAYS` | `7` | max date gap inside one event |
| `LAYA_QUALIFY_THRESHOLD` | `0.7` | P(fit) and P(attribution) needed for a strong positive |
| `LAYA_REJECT_THRESHOLD` | `0.3` | at or below → semantic negative |
| `LLM_REVIEW_ENABLED` | `false` | route REVIEW cases to the LLM reviewer |
| `LLM_REVIEW_MIN_CONFIDENCE` | `0.75` | reviewer confidence needed to change a REVIEW |

## 3. Hard rules

Each rule returns `pass` / `fail` / `unknown` / `not_applicable` with a reason and the signal
ids it used. **`unknown` never counts as a pass.**

* **Identity** — company `domain_status = verified` **and** a canonical domain. Otherwise
  `fail` → NOT_QUALIFIED (blocking). Unverified companies can never be QUALIFIED.
* **Geography** — explicit `country_code` (match → pass, other → **fail, blocking**); else the
  verified domain's ccTLD; else attributed evidence naming the country/major cities or INR
  amounts; else `unknown` (caps the decision at REVIEW).
* **Funding path (A)** — over funding EVENTS (not raw signals):
  * date missing → unknown; older than the window → fail (stale);
  * attribution only by name → unknown (namesake risk);
  * round amount: explicit unit required; valuation / revenue / share-price numbers ignored;
    cumulative totals ("so far", "total funding") never used as the round amount;
    USD only with `ICP_USD_TO_INR_RATE`; missing → unknown;
  * publishers disagreeing on the amount (>10 %) → unknown (conflict);
  * amount ≤ cap → **pass**; > cap → fail and `over_cap_recent`;
  * a passing round AND a recent above-cap round → unknown (conflict).
* **Intent path (B)** — an event of type ai_interest / expansion / product_customer /
  technology / pain_point (hiring only together with another intent cue), inside the window,
  attributed beyond a bare name match, confidence ≥ `ICP_MIN_SIGNAL_CONFIDENCE` → **pass**.
  Weak, name-only, low-confidence or hiring-only evidence → fail.
* The paths are **independent**: a company passes the hard gate with identity + geography pass
  and at least one path pass.

## 4. Event clustering

One item = one source URL (may carry several signal types). Items are the same event when
their dates are ≤ `CLUSTER_WINDOW_DAYS` apart and (headline Jaccard ≥ threshold, company-name
words removed) OR (same funding round amount) OR (a shared significant number, not a year, plus
two shared key words). Dateless items merge only on near-identical headlines (≥ 0.8). Cluster
confidence = max member confidence (corroboration is recorded as publisher count, never as
extra confidence). **No signal is deleted**; every signal id stays in the event and in the
persisted bundle.

## 5. Laya questions (typed, `app/domain/qualification/bundle.py`)

| Dimension | Laya type | Answer |
|---|---|---|
| `business_trigger` | choice | funding / ai_automation / expansion / customer_deployment / hiring_growth / none |
| `attribution` | noul (P(true)) | the events describe THIS company (not a namesake) |
| `company_fit` | noul | good prospect now, based on concrete recent activity — the decision **score** |
| `evidence_quality` | score 0–2 | insufficient / partial / sufficient |

Verdict: **negative** if attribution ≤ reject threshold, or fit ≤ reject threshold, or trigger
= none with calibrated confidence ≥ qualify threshold; **strong positive** if fit ≥ qualify,
attribution ≥ qualify, evidence level ≥ 1 and trigger ≠ none; otherwise **ambiguous**.

## 6. Final decision precedence

1. identity or geography **fail** → NOT_QUALIFIED
2. no path passes: a path `unknown` → REVIEW; otherwise NOT_QUALIFIED
3. intent-only + recent above-cap round: `reject` → NOT_QUALIFIED; `review` → at most REVIEW
4. geography unknown → at most REVIEW
5. Laya error/timeout → REVIEW
6. Laya negative → NOT_QUALIFIED; strong positive (and not capped) → QUALIFIED; else REVIEW
7. REVIEW + `LLM_REVIEW_ENABLED` → LLM review. Accepted only if confidence ≥
   `LLM_REVIEW_MIN_CONFIDENCE` **and** it cites evidence ids that exist in the bundle **and** it
   does not claim QUALIFIED where the hard gate forbids it (`qualified_allowed = false`).

Database guards: `decision = 'qualified'` requires `hard_rules_passed` (CHECK constraint);
decisions are append-only (UPDATE rejected by trigger).

## 7. Phase 6 contract

`ensure_enrichment_allowed(store, company_id)` passes only when the company's **latest**
decision is QUALIFIED, hard rules passed, and it was made under the **current** policy version.
REVIEW and NOT_QUALIFIED never reach enrichment; a future human approval must record a new
decision, not bypass the gate.
