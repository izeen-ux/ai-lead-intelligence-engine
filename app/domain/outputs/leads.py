"""Lead view (one lead per company) and its Google Sheets row mapping.

``lead_id`` is ``lead_`` + the first 16 hex characters of the company UUID: stable across
re-qualification, re-enrichment and re-analysis, so repeated exports update the same row.

Every row carries the ids it was built from (company / qualification / contact / analysis)
and the evidence sources, so a reader can trace why a lead is qualified and why an offer was
chosen. Values are the stored values - nothing is invented or embellished.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from app.domain.analysis.catalog import ServiceCatalog
from app.domain.analysis.records import AnalysisStatus, LeadAnalysisRecord
from app.domain.enrichment.records import EnrichmentContactRecord
from app.domain.enums import ContactStatus, QualificationDecision
from app.domain.outputs.profile import NOT_FOUND, CompanyProfile, funding_of
from app.domain.qualification.records import QualificationDecisionRecord

CELL_LIMIT = 2000  # far below the Sheets 50k-per-cell limit; keeps rows readable
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ExportStatus(StrEnum):
    READY = "ready"  # verified contact + succeeded analysis
    AWAITING_CONTACT = "awaiting_contact"  # qualified, no verified contact yet
    AWAITING_ANALYSIS = "awaiting_analysis"  # verified contact, no accepted analysis yet
    NEEDS_REVIEW = "needs_review"  # hard rules pass, a human decision is pending
    WITHDRAWN = "withdrawn"  # was exported, latest decision is no longer QUALIFIED


# Every field the app can project for a lead. Column A of any sheet layout is ``lead_id`` (the
# upsert key). This full list was the "Ai Leads" header until 2026-10-06 (39 columns).
LEAD_FIELDS: tuple[str, ...] = (
    "lead_id",
    "lead_status",  # qualified | needs review | withdrawn
    "company_name",
    "website",
    "company_location",
    "industry",
    "founder_name",
    "founder_title",
    "founder_email",
    "email_status",
    "founder_phone",
    "phone_status",
    "founder_linkedin_url",
    "company_linkedin_url",
    "funding_amount",
    "funding_round",
    "funding_date",
    "qualification_decision",
    "qualification_path",
    "qualification_confidence",
    "primary_trigger",
    "verified_contact",
    "ai_opportunity",
    "recommended_offer",
    "offer_match_reason",
    "outreach_hook",
    "strongest_signal_date",
    "evidence_sources",
    "qualification_id",
    "contact_id",
    "analysis_id",
    "last_updated_at",
    "export_status",
    # --- traceability / detail (kept from layout v1) ---
    "company_id",
    "observed_facts",
    "pain_hypotheses",
    "analysis_confidence",
    "evidence_count",
    "field_provenance",
    "status_reasons",
    "lead_owner",
)

# What the "Ai Leads" worksheet shows (owner decision 2026-10-06: fewer, clearer columns).
# Ids, provenance and analysis detail stay in PostgreSQL and the dashboard.
LEADS_COLUMNS: tuple[str, ...] = (
    "lead_id",
    "lead_status",  # qualified | needs review | withdrawn
    "company_name",
    "website",
    "company_location",
    "industry",
    "founder_name",
    "founder_title",
    "founder_email",
    "founder_phone",
    "founder_linkedin_url",
    "funding_amount",
    "funding_round",
    "primary_trigger",
    "recommended_offer",
    "outreach_hook",
    "evidence_sources",
    "status_reasons",
    "last_updated_at",
    "lead_owner",  # team member whose run produced the lead
)
assert set(LEADS_COLUMNS) <= set(LEAD_FIELDS)  # noqa: S101 - import-time layout check

# A worksheet still carrying the 39-column header is converted in place by the exporter: each
# row keeps its values by column NAME, dropped columns are cleared (see ``sync.relayout``).
LEGACY_LEADS_COLUMNS: tuple[str, ...] = tuple(f for f in LEAD_FIELDS if f != "lead_owner")
LEADS_RENAMES: dict[str, str] = {}


def lead_id_for(company_id: UUID) -> str:
    return f"lead_{company_id.hex[:16]}"


def iso(value: datetime | None) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds") if value else ""


def clean(value: object) -> str:
    """One safe cell: text only, no control characters, bounded length. Written with
    valueInputOption=RAW so a leading '=' is never evaluated as a formula."""
    if value is None:
        return ""
    text = _CONTROL.sub(" ", str(value)).strip()
    return text if len(text) <= CELL_LIMIT else text[: CELL_LIMIT - 1] + "…"


def row_hash(cells: list[str]) -> str:
    return hashlib.sha256("\x1f".join(cells).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LeadView:
    """Current state of one company, assembled from the stored records (read-only)."""

    company_id: UUID
    company_name: str
    domain: str | None
    domain_status: str
    decision: QualificationDecisionRecord
    contact: EnrichmentContactRecord | None = None
    analysis: LeadAnalysisRecord | None = None  # latest SUCCEEDED for this decision + contact
    latest_analysis: LeadAnalysisRecord | None = None  # latest of any status (diagnostics)
    profile: CompanyProfile = field(default_factory=CompanyProfile)
    owner: str = ""  # team member whose pipeline run produced this lead ('' = unassigned)

    @property
    def lead_id(self) -> str:
        return lead_id_for(self.company_id)

    @property
    def events(self) -> list[dict[str, Any]]:
        events = self.decision.evidence_bundle.get("events") or []
        return [e for e in events if isinstance(e, dict)]

    @property
    def strongest_signal_date(self) -> str:
        """Date of the first bundle event (qualifying events come first, newest first)."""
        return str(self.events[0].get("date") or "") if self.events else ""

    @property
    def evidence_sources(self) -> list[str]:
        seen: dict[str, None] = {}
        for event in self.events:
            for url in event.get("sources") or []:
                seen.setdefault(str(url), None)
        return list(seen)

    @property
    def funding(self) -> dict[str, str]:
        """amount / round / date / source of the decision's funding event ('' when none)."""
        return funding_of(self.events, self.profile.funding_round_hint)

    @property
    def field_provenance(self) -> dict[str, str]:
        """Where each exported contact / company field came from (``NOT_FOUND`` when the
        stored evidence has no value - blank cells are legitimate, never filled by guessing)."""
        c = self.contact
        cp: dict[str, Any] = c.provenance if c is not None else {}
        email: dict[str, Any] = cp.get("email") or {}
        phone: dict[str, Any] = cp.get("phone") or {}
        out = {
            "founder_name": ", ".join((cp.get("identity") or {}).get("sources") or []) or NOT_FOUND,
            "founder_title": str(cp.get("title") or NOT_FOUND),
            "founder_email": (
                f"{email.get('provider')} (confidence {email.get('confidence')})"
                if c is not None and c.email and email.get("provider")
                else NOT_FOUND
            ),
            "founder_phone": (
                f"{phone.get('provider')}"
                if c is not None and c.phone
                else (c.phone_status.value if c is not None else NOT_FOUND)
            ),
            "founder_linkedin_url": str(cp.get("linkedin_url") or NOT_FOUND)
            if c is not None and c.linkedin_url
            else NOT_FOUND,
            **self.profile.provenance,
            "funding": self.funding["source"],
        }
        return out

    @property
    def contact_verified(self) -> bool:
        return self.contact is not None and self.contact.status is ContactStatus.VERIFIED

    @property
    def in_sheet(self) -> bool:
        """Belongs in the Ai Leads tab: QUALIFIED, or REVIEW with passing hard ICP rules."""
        d = self.decision
        return d.decision is QualificationDecision.QUALIFIED or (
            d.decision is QualificationDecision.REVIEW and d.hard_rules_passed
        )

    @property
    def lead_status(self) -> str:
        if self.decision.decision is QualificationDecision.QUALIFIED:
            return "qualified"
        return "needs review" if self.in_sheet else "withdrawn"

    @property
    def export_status(self) -> ExportStatus:
        if self.decision.decision is not QualificationDecision.QUALIFIED:
            return ExportStatus.NEEDS_REVIEW if self.in_sheet else ExportStatus.WITHDRAWN
        if not self.contact_verified:
            return ExportStatus.AWAITING_CONTACT
        if self.analysis is None:
            return ExportStatus.AWAITING_ANALYSIS
        return ExportStatus.READY

    @property
    def last_updated_at(self) -> datetime:
        stamps = [self.decision.decided_at]
        if self.contact is not None:
            stamps.append(self.contact.created_at)
        if self.analysis is not None:
            stamps.append(self.analysis.analyzed_at)
        return max(stamps)

    @property
    def contact_state(self) -> str:
        if self.contact is None:
            return "none"
        name = self.contact.full_name or "unnamed"
        return f"{self.contact.status.value}: {name} ({self.contact.title or '-'})"

    @property
    def analysis_state(self) -> str:
        if self.analysis is not None:
            return f"succeeded: {self.analysis.recommended_offer_id}"
        if self.latest_analysis is not None:
            a = self.latest_analysis
            return f"{a.status.value}" + (f" ({a.error_code})" if a.error_code else "")
        return "none"


def _bullets(items: list[dict[str, Any]], text_key: str, limit: int = 3) -> str:
    lines = []
    for item in items[:limit]:
        ids = ", ".join(str(i) for i in item.get("evidence_ids") or [])
        lines.append(f"• {item.get(text_key, '')}" + (f" [{ids}]" if ids else ""))
    return "\n".join(lines)


def _offer(offer_id: str | None, catalog: ServiceCatalog) -> str:
    if not offer_id:
        return ""
    offer = catalog.get(offer_id)
    return f"{offer.name} ({offer_id})" if offer is not None else offer_id


def lead_cells(
    view: LeadView, catalog: ServiceCatalog, columns: tuple[str, ...] = LEADS_COLUMNS
) -> list[str]:
    a = view.analysis
    out: dict[str, Any] = (a.analysis or {}) if a is not None else {}
    c = view.contact
    verified = view.contact_verified
    status = view.export_status
    funding = view.funding
    values: dict[str, object] = {
        "lead_id": view.lead_id,
        "lead_status": view.lead_status,
        "company_name": view.company_name,
        "website": view.domain if view.domain_status == "verified" else "",
        "company_location": view.profile.location,
        "industry": view.profile.industry,
        "founder_name": c.full_name if c is not None else "",
        "founder_title": c.title if c is not None else "",
        # only a VERIFIED work email is exported; anything else stays in PostgreSQL
        "founder_email": c.email if (c is not None and verified) else "",
        "email_status": c.email_verification.value if c is not None else "",
        # phone / LinkedIn are optional: blank + a status, never a guessed value
        "founder_phone": c.phone if (c is not None and verified) else "",
        "phone_status": c.phone_status.value if c is not None else "",
        "founder_linkedin_url": c.linkedin_url if c is not None else "",
        "company_linkedin_url": view.profile.company_linkedin_url,
        "funding_amount": funding["amount"],
        "funding_round": funding["round"],
        "funding_date": funding["date"],
        "qualification_decision": view.decision.decision.value,
        "qualification_path": view.decision.path.value,
        "qualification_confidence": f"{view.decision.confidence:.2f}"
        if view.decision.confidence is not None
        else "",
        "primary_trigger": a.primary_trigger if a is not None else "",
        "verified_contact": "yes" if verified else "no",
        "ai_opportunity": _bullets(out.get("ai_opportunities") or [], "description", limit=1),
        "recommended_offer": _offer(a.recommended_offer_id if a else None, catalog),
        "offer_match_reason": out.get("offer_match_reason", ""),
        "outreach_hook": out.get("outreach_hook", ""),
        "strongest_signal_date": view.strongest_signal_date,
        "evidence_sources": "\n".join(view.evidence_sources[:5]),
        "qualification_id": view.decision.id,
        "contact_id": c.id if c is not None else "",
        "analysis_id": a.id if a is not None else "",
        "last_updated_at": iso(view.last_updated_at),
        "export_status": status.value,
        "company_id": view.company_id,
        "observed_facts": _bullets(out.get("observed_facts") or [], "statement"),
        "pain_hypotheses": _bullets(out.get("pain_hypotheses") or [], "statement"),
        "analysis_confidence": f"{a.confidence:.2f}" if a and a.confidence is not None else "",
        "evidence_count": view.decision.event_count,
        "field_provenance": "\n".join(f"{k}: {v}" for k, v in view.field_provenance.items()),
        # why it is (not yet) a lead: the decision's own reasons, newest decision only
        "status_reasons": "\n".join(view.decision.reasons[:4]),
        "lead_owner": view.owner,
    }
    if status in (ExportStatus.WITHDRAWN, ExportStatus.NEEDS_REVIEW):
        # not (or no longer) QUALIFIED: keep identity + reasons, never show outreach material
        for key in (
            "founder_email", "founder_phone", "outreach_hook", "recommended_offer",
            "ai_opportunity",
        ):  # fmt: skip
            values[key] = ""
    return [clean(values[col]) for col in columns]


def analysis_is_current(a: LeadAnalysisRecord, view_decision_id: UUID, contact_id: UUID) -> bool:
    return (
        a.status is AnalysisStatus.SUCCEEDED
        and a.qualification_id == view_decision_id
        and a.contact_id == contact_id
    )
