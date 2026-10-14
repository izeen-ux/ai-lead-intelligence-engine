"""Assemble read-only lead views from the stored Phase 5/6/7 records (Phase 8).

Selection rules (documented in docs/phases/OUTPUTS_POLICY.md):
  decision  = latest qualification decision of the company
  contact   = newest contact enriched under THAT decision: VERIFIED first, else the newest
              named person (shown with its status; its email is never exported)
  analysis  = newest SUCCEEDED analysis for that decision AND that contact
  profile   = location / industry / company LinkedIn derived from stored company facts
              (app/domain/outputs/profile.py); empty when the evidence has no value
Calls no provider and writes nothing.
"""

from __future__ import annotations

from uuid import UUID

from app.core.errors import NotFoundError
from app.domain.enrichment.records import EnrichmentContactRecord
from app.domain.enums import ContactStatus
from app.domain.outputs.leads import LeadView, analysis_is_current
from app.domain.outputs.profile import build_profile
from app.domain.qualification.records import QualificationDecisionRecord
from app.repositories.analysis import AnalysisStore
from app.repositories.enrichment import EnrichmentStore
from app.repositories.qualification import QualificationStore


def pick_contact(
    contacts: list[EnrichmentContactRecord], decision: QualificationDecisionRecord
) -> EnrichmentContactRecord | None:
    """``contacts`` are newest first (store contract)."""
    under = [c for c in contacts if c.qualification_id == decision.id and c.full_name]
    verified = [c for c in under if c.status is ContactStatus.VERIFIED]
    if verified:
        return verified[0]
    return under[0] if under else None


class LeadViewService:
    def __init__(
        self,
        qualifications: QualificationStore,
        enrichment: EnrichmentStore,
        analyses: AnalysisStore,
    ) -> None:
        self._q = qualifications
        self._e = enrichment
        self._a = analyses

    def view(self, company_id: UUID) -> LeadView | None:
        """None when the company has never been through qualification."""
        decision = self._q.latest_decision(company_id)
        if decision is None:
            return None
        try:
            ctx = self._e.load_company(company_id)
            name, domain, domain_status = ctx.name, ctx.domain, ctx.domain_status.value
            profile = build_profile(
                location=ctx.location,
                country_code=ctx.country_code,
                industry=ctx.industry,
                description=ctx.description,
                social_links=ctx.social_links,
                funding_round=ctx.funding_round,
                hard_rules=decision.hard_rule_results,
            )
        except NotFoundError:
            name, domain, domain_status = decision.company_name, None, "unknown"
            profile = build_profile(hard_rules=decision.hard_rule_results)
            ctx = None
        contact = pick_contact(self._e.contacts(company_id), decision)
        history = self._a.analyses(company_id)  # newest first
        analysis = None
        if contact is not None:
            analysis = next(
                (a for a in history if analysis_is_current(a, decision.id, contact.id)), None
            )
        return LeadView(
            company_id=company_id,
            company_name=name,
            domain=domain,
            domain_status=domain_status,
            decision=decision,
            contact=contact,
            analysis=analysis,
            latest_analysis=history[0] if history else None,
            profile=profile,
            owner=(ctx.owner or "") if ctx is not None else "",
        )

    def views(self, company_ids: list[UUID]) -> list[LeadView]:
        return [v for v in (self.view(c) for c in company_ids) if v is not None]
