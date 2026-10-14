"""Phase 6 enrichment - pure, deterministic domain logic (no I/O, no vendors).

* ``config``   - EnrichmentConfig (thresholds, budgets, TTLs, role priority) + policy version
* ``identity`` - person/founder identity package from attributable sources; ambiguity handling
* ``policy``   - provider-result acceptance (person/domain match), merge with provenance,
                 verification requirements, final contact status
* ``budget``   - per-lead / per-run / per-month / sidecar-call ledger
* ``records``  - persistable run / contact / attempt / cache records
"""
