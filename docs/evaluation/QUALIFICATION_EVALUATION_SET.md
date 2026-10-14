# Qualification Evaluation Set (Phase 5)

**File:** `evaluation/qualification/labeled_cases.json` (dataset `qualification-labeled-cases`,
version `2026-09-30.1`, reference date `2026-09-30T12:00Z`)
**Harness:** `app/evaluation/qualification.py` · CLI `scripts/evaluate_qualification.py`
**Reports:** `evaluation/qualification/reports/`

> **This is a SYNTHETIC set.** Company names, domains and URLs are invented. It was written by
> the implementer to exercise the policy's decision boundaries. Results on it are **not**
> production accuracy and must not be quoted as such.

## 1. Three kinds of data, kept apart

| Kind | Where | Purpose |
|---|---|---|
| Unit-test fixtures | `tests/qualification_factories.py`, inline in `tests/unit/test_qualification_*.py` | Prove individual rules/boundaries; pass/fail |
| Labeled evaluation cases | `evaluation/qualification/labeled_cases.json` | Measure the whole gate end to end (mock or live Laya) |
| Production data | PostgreSQL `lead_intel` (companies, signals, qualification_*) | Real decisions; never used as labels without manual review |

The harness loads cases into an **in-memory** store and runs the real `QualificationService`
with `persist=False` and a fixed clock (the reference date). It never touches the database.

## 2. Labels

| Field | Meaning |
|---|---|
| `label` | expected final decision: `qualified` / `review` / `not_qualified`, assuming a competent semantic judge and the documented policy (`docs/phases/QUALIFICATION_POLICY.md`) |
| `hard_path` | expected deterministic path: `funding` / `intent` / `both` / `none` |
| `expected_events` | (duplicate-coverage cases) expected event count after clustering |
| `category` | scenario family (below) |
| `rationale` | one line explaining the label |

Signals are given as `{source, type, title, platform, days_ago, confidence, attributed}`;
`attributed=true` adds the Phase 4 note "matches verified-website context"; `days_ago: null`
means no publication date.

## 3. Coverage (28 cases, 2 per category)

| Category | Cases | Expected |
|---|---|---|
| clear_funding | Q01, Q02 | qualified |
| funding_above_cap (₹45 Cr; ₹5.2 Cr boundary) | Q03, Q04 | not_qualified |
| funding_outside_window (120 d; 91 d boundary) | Q05, Q06 | not_qualified |
| missing_funding_amount (no amount; cumulative only) | Q07, Q08 | review |
| strong_ai_intent | Q09, Q10 | qualified |
| strong_expansion | Q11, Q12 | qualified |
| weak_generic_news (hiring-only; listicle) | Q13, Q14 | not_qualified |
| irrelevant_mention | Q15, Q16 | not_qualified |
| namesake_noisy (game; irrelevant YouTube) | Q17, Q18 | not_qualified |
| duplicate_coverage (4 outlets, ₹4.5 Cr; 4 outlets, ₹45 Cr) | Q19, Q20 | qualified / not_qualified, 1 event each |
| conflicting_evidence (₹4 vs ₹40 Cr; ₹3 Cr + ₹50 Cr rounds) | Q21, Q22 | review |
| insufficient_evidence (no signals; no date) | Q23, Q24 | not_qualified / review |
| ambiguous_review (Drivn-like over-cap + intent; unknown geography) | Q25, Q26 | review |
| identity_geography (unverified; country US) | Q27, Q28 | not_qualified |

Label distribution: 7 qualified, 7 review, 14 not_qualified.

## 4. Metrics reported

decision accuracy · confusion matrix (label × predicted) · hard-path accuracy · event-count
checks · **false QUALIFIED** (the costly error: it would unlock enrichment spend) · per-category
results · every mismatch with its reasons.

## 5. Results (2026-09-30)

| Run | Decision accuracy | Hard-path accuracy | Events | False QUALIFIED | Report |
|---|---|---|---|---|---|
| mock Laya (deterministic stand-in) | 28/28 (1.00) | 1.00 | 2/2 | 0 | `reports/mock_2026-09-30.json` |
| **live Laya** 0.3.22 typed-decisions, CPU | **21/28 (0.75)** | 1.00 | 2/2 | **0** | `reports/live_laya_2026-09-30.json` |

Live confusion (label → predicted): qualified → review ×7; review → review ×7;
not_qualified → not_qualified ×14. All 7 misses are "qualified" cases that live Laya scored
ambiguous (P(fit) 0.62–0.66, attribution 0.55–0.62, below the 0.7 threshold), so they became
REVIEW. The mock result only shows that the deterministic pipeline reproduces the labels; it
says nothing about Laya.

**Laya standalone separation** (`reports/laya_separation_2026-09-30.json`: every case's bundle
sent to Laya even when hard rules already decided it, JSON vs text state): P(fit) ranged
0.57–0.69 and P(attribution) 0.48–0.68 for positives and negatives alike (e.g. namesake Q17 fit
0.57, strong Q09 fit 0.64). A second wording experiment (10 cases) gave overlapping
distributions too. The `business_trigger` choice was sensible (funding / ai_automation /
expansion). Conclusion: out of the box, Laya adds an explanation (trigger) but not a reliable
fit/attribution discrimination for this task; thresholds were **not** tuned on this synthetic set.

## 6. How to extend (needed before trusting accuracy)

1. Collect ≥ 50–200 real companies that reached VERIFIED + signal research (today: 1 - Drivn).
2. Label each manually (decision + path + short rationale) in a **new** file, e.g.
   `evaluation/qualification/real_labeled_<date>.json`, same schema; never edit labels to match
   model output.
3. Run `scripts/evaluate_qualification.py --dataset <file> [--live]`; keep the reports.
4. Only then consider calibrating thresholds or fine-tuning the typed-decisions checkpoint, and
   bump `POLICY_VERSION` when anything changes.
