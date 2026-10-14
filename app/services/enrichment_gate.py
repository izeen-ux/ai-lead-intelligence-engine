"""The single door from qualification into Phase 6 enrichment.

Phase 6 (Hunter / Apollo / enrichment-kit) MUST call ``ensure_enrichment_allowed`` before
spending any credit. Only a company whose LATEST decision is QUALIFIED, with passed hard rules,
made under the CURRENT policy version - and, when ``icp`` is given, under the CURRENT ICP
configuration - may pass. REVIEW and NOT_QUALIFIED never pass; a human approval flow (Phase 8)
would record a new decision rather than bypass this check.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.core.errors import EnrichmentNotAllowedError
from app.domain.qualification.icp import POLICY_VERSION, IcpConfig
from app.domain.qualification.records import QualificationDecisionRecord
from app.repositories.qualification import QualificationStore


def _comparable(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in snapshot.items() if k != "policy_version"}


def ensure_enrichment_allowed(
    store: QualificationStore, company_id: UUID, icp: IcpConfig | None = None
) -> QualificationDecisionRecord:
    latest = store.latest_decision(company_id)
    details = {"company_id": str(company_id)}
    if latest is None:
        raise EnrichmentNotAllowedError("company has never been qualified", details=details)
    if not latest.enrichment_eligible:
        raise EnrichmentNotAllowedError(
            f"latest qualification decision is {latest.decision.value}",
            details={**details, "decision_id": str(latest.id)},
        )
    if latest.policy_version != POLICY_VERSION:
        raise EnrichmentNotAllowedError(
            "qualification was made under an older policy version; re-qualify first",
            details={**details, "policy_version": latest.policy_version},
        )
    if icp is not None and _comparable(latest.icp_config) != _comparable(icp.snapshot()):
        raise EnrichmentNotAllowedError(
            "qualification was made under a different ICP configuration; re-qualify first",
            details={**details, "decision_id": str(latest.id)},
        )
    return latest
