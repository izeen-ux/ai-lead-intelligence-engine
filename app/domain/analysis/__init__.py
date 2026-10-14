"""Phase 7 AI lead analysis - pure, deterministic domain logic (no I/O, no vendors).

* ``catalog``  - the versioned TechnoTribes service catalog (nine offers; no invented services)
* ``config``   - AnalysisConfig (model policy, budgets) + policy/prompt versions
* ``bundle``   - compact evidence bundle with stable evidence ids (deduplicated events)
* ``output``   - strict structured output (FACT / INFERENCE / HYPOTHESIS / RECOMMENDATION)
                 and grounding validation (evidence ids, offer id)
* ``prompt``   - the versioned prompt
* ``records``  - immutable analysis records
"""
