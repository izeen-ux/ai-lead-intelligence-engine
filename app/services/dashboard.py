"""Dashboard read models (Phase 8). Reads through the stores / lead views only.

Provider health uses each adapter's ``status()`` - configuration state only, NO network call
and therefore no paid call. Usage/cost comes from stored records and is labelled
``provider_reported`` or ``ledger_estimate``; provider account counters are not visible here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from app.core.config import Settings
from app.core.errors import NotFoundError
from app.domain.analysis.catalog import ServiceCatalog, default_catalog
from app.domain.enums import QualificationDecision
from app.domain.models import utcnow
from app.domain.outputs.leads import LeadView, iso, lead_id_for
from app.providers.contracts import ProviderStatus
from app.repositories.outputs import OutputStore
from app.repositories.qualification import QualificationStore
from app.services.leads import LeadViewService
from app.services.pipeline import PipelineService, run_summary
from app.services.sheets_export import FIXTURE_LABEL

PROVIDER_ORDER = (
    "apify", "firecrawl", "crawl4ai", "agent_reach", "laya",
    "enrichment_kit", "hunter", "apollo", "openai", "anthropic", "ai_router",
)  # fmt: skip


def month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


@dataclass(frozen=True)
class DashboardBackend:
    """Everything the dashboard reads/writes through (SQL in production, in-memory in demo)."""

    outputs: OutputStore
    views: LeadViewService
    qualifications: QualificationStore
    label: str  # "postgresql" | "demo (in-memory)"
    pipeline: PipelineService | None = None  # None = pipeline controls unavailable
    # True for the in-memory demo and the ``lead_intel_demo`` database: every company name
    # and contact value shown is labelled as a synthetic fixture.
    synthetic: bool = False


def _mark(value: str | None, synthetic: bool) -> str | None:
    return f"{FIXTURE_LABEL}{value}" if (synthetic and value) else value


def lead_summary(v: LeadView, catalog: ServiceCatalog, synthetic: bool = False) -> dict[str, Any]:
    offer = v.analysis.recommended_offer_id if v.analysis else None
    o = catalog.get(offer) if offer else None
    return {
        "lead_id": v.lead_id,
        "company_id": str(v.company_id),
        "company_name": _mark(v.company_name, synthetic),
        "owner": v.owner or None,
        "synthetic": synthetic,
        "domain": v.domain,
        "domain_status": v.domain_status,
        "decision": v.decision.decision.value,
        "path": v.decision.path.value,
        "decided_at": iso(v.decision.decided_at),
        "reasons": v.decision.reasons[:3],
        "contact": _mark(v.contact_state, synthetic and v.contact is not None),
        "analysis": v.analysis_state,
        "recommended_offer": o.name if o is not None else offer,
        "export_status": v.export_status.value if v.in_sheet else "not in the sheet",
        "strongest_signal_date": v.strongest_signal_date,
    }


class DashboardService:
    def __init__(
        self,
        backend: DashboardBackend,
        statuses: Callable[[], list[ProviderStatus]],
        settings: Settings,
        *,
        catalog: ServiceCatalog | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._b = backend
        self._statuses = statuses
        self._settings = settings
        self._catalog = catalog or default_catalog()
        self._clock = clock

    # ------------------------------------------------------------------ overview
    def provider_health(self) -> list[dict[str, Any]]:
        by_name = {s.name.value: s for s in self._statuses()}
        out = []
        for name in PROVIDER_ORDER:
            s = by_name.get(name)
            if s is None:
                continue
            out.append(
                {
                    "provider": name,
                    "mode": s.mode,
                    "configured": s.configured,
                    "operational": s.operational,
                    "paid": s.paid,
                    "detail": s.detail,
                    "checked": "configuration only (no network call)",
                }
            )
        sheets_ok = bool(self._settings.google_sheets_spreadsheet_id) and (
            self._settings.google_service_account_json.is_file()
        )
        out.append(
            {
                "provider": "google_sheets (output)",
                "mode": "live" if self._settings.allow_sheets_writes else "writes disabled",
                "configured": sheets_ok,
                "operational": sheets_ok,
                "paid": False,
                "detail": None if sheets_ok else "spreadsheet id or service-account key missing",
                "checked": "configuration only (no network call)",
            }
        )
        return out

    async def overview(self) -> dict[str, Any]:
        now = self._clock()
        counts = await asyncio.to_thread(self._b.outputs.pipeline_counts)
        usage = await asyncio.to_thread(self._b.outputs.usage_summary, month_start(now))
        runs = await asyncio.to_thread(self._b.outputs.export_runs, 5)
        pipeline_runs = await self._b.pipeline.runs(5) if self._b.pipeline else []
        return {
            "pipeline_runs": {
                "available": self._b.pipeline is not None,
                "mode": self._b.pipeline.mode if self._b.pipeline else None,
                "writes_allowed": self._b.pipeline.writes_allowed() if self._b.pipeline else False,
                "schedule_interval_minutes": self._settings.pipeline_schedule_interval_minutes,
                "runs": [run_summary(r) for r in pipeline_runs],
            },
            "backend": self._b.label,
            "generated_at": iso(now),
            "pipeline": asdict(counts),
            "providers": self.provider_health(),
            "usage": [
                {**asdict(u), "all_time": str(u.all_time), "this_month": str(u.this_month)}
                for u in usage
            ],
            "exports": [
                {
                    "run_id": str(r.id),
                    "mode": r.mode.value,
                    "status": r.status.value,
                    "started_at": iso(r.started_at),
                    "inserted": r.inserted_count,
                    "updated": r.updated_count,
                    "unchanged": r.unchanged_count,
                    "withdrawn": r.withdrawn_count,
                    "failed": r.failed_count,
                    "error_code": r.error_code,
                }
                for r in runs
            ],
        }

    # ------------------------------------------------------------------ lists
    async def leads(
        self, decision: QualificationDecision | None, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        if decision is None:
            ids = await asyncio.to_thread(self._b.outputs.all_company_ids, limit, offset)
        else:
            ids = await asyncio.to_thread(
                self._b.outputs.company_ids_by_decision, decision, limit, offset
            )
        views = await asyncio.to_thread(self._b.views.views, ids)
        return [lead_summary(v, self._catalog, self._b.synthetic) for v in views]

    async def review_queue(self, limit: int, offset: int) -> list[dict[str, Any]]:
        return await self.leads(QualificationDecision.REVIEW, limit, offset)

    # ------------------------------------------------------------------ detail
    async def lead_detail(self, company_id: UUID) -> dict[str, Any]:
        view = await asyncio.to_thread(self._b.views.view, company_id)
        if view is None:
            raise NotFoundError("no qualification decision for this company",
                                details={"id": str(company_id)})  # fmt: skip
        history = await asyncio.to_thread(self._b.qualifications.history, company_id)
        reviews = await asyncio.to_thread(self._b.outputs.human_reviews, company_id)
        export = await asyncio.to_thread(self._b.outputs.last_export_item, lead_id_for(company_id))
        d, c, a = view.decision, view.contact, view.analysis
        fake = self._b.synthetic
        offer = self._catalog.get(a.recommended_offer_id) if a and a.recommended_offer_id else None
        return {
            "summary": lead_summary(view, self._catalog, self._b.synthetic),
            "trace": {
                "company_id": str(company_id),
                "qualification_id": str(d.id),
                "contact_id": str(c.id) if c else None,
                "analysis_id": str(a.id) if a else None,
            },
            "qualification": {
                "decision": d.decision.value,
                "path": d.path.value,
                "hard_rules_passed": d.hard_rules_passed,
                "hard_rules": d.hard_rule_results,
                "reasons": d.reasons,
                "laya": d.laya_result,
                "policy_version": d.policy_version,
                "decided_at": iso(d.decided_at),
                "events": view.events,
                "human_review": d.evidence_bundle.get("human_review"),
            },
            "contact": None
            if c is None
            else {
                "name": _mark(c.full_name, fake),
                "title": c.title,
                "status": c.status.value,
                "email": _mark(c.email if view.contact_verified else None, fake),
                "email_verification": c.email_verification.value,
                "provider": c.provider.value if c.provider else None,
                "sources": (c.identity or {}).get("sources"),
                # optional fields: None + a status when the evidence has no value
                "phone": _mark(c.phone if view.contact_verified else None, fake),
                "phone_status": c.phone_status.value,
                "linkedin_url": _mark(c.linkedin_url, fake),
            },
            "company": {
                "location": view.profile.location or None,
                "industry": view.profile.industry or None,
                "company_linkedin_url": _mark(view.profile.company_linkedin_url or None, fake),
                "funding": {k: v or None for k, v in view.funding.items() if k != "source"},
                "field_provenance": view.field_provenance,
            },
            "analysis": None
            if a is None
            else {
                "offer": {"id": a.recommended_offer_id, "name": offer.name if offer else None},
                "primary_trigger": a.primary_trigger,
                "confidence": a.confidence,
                "output": a.analysis,
                "supporting_evidence_ids": a.supporting_evidence_ids,
                "evidence": a.evidence_bundle,
                "model": a.model,
                "versions": {
                    "policy": a.policy_version,
                    "prompt": a.prompt_version,
                    "catalog": a.catalog_version,
                },
            },
            "latest_analysis_state": view.analysis_state,
            "decision_history": [
                {
                    "id": str(h.id),
                    "decision": h.decision.value,
                    "path": h.path.value,
                    "decided_at": iso(h.decided_at),
                    "reason": h.reasons[0] if h.reasons else "",
                }
                for h in history
            ],
            "reviews": [
                {
                    "action": r.action.value,
                    "outcome": r.outcome.value,
                    "reviewer": r.reviewer,
                    "reason": r.reason,
                    "refusal_code": r.refusal_code,
                    "reviewed_at": iso(r.reviewed_at),
                    "resulting_decision_id": str(r.resulting_decision_id)
                    if r.resulting_decision_id
                    else None,
                }
                for r in reviews
            ],
            "export": None
            if export is None
            else {
                "action": export.action.value,
                "row_number": export.row_number,
                "export_status": export.export_status,
                "at": iso(export.created_at),
            },
        }
