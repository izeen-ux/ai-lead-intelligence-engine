"""Pre-screen persistence: candidates from stored discovery data, insert-only decisions, and
the list of companies website intelligence may spend credits on."""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text
from sqlalchemy.orm import sessionmaker

from app.db.models import PrescreenDecisionRow
from app.domain.prescreen import PRESCREEN_POLICY_VERSION, DiscoveryFacts, PrescreenOutcome

# Unresolved companies never pre-screened, oldest first, with their first discovery hit.
_CANDIDATES = text(
    "SELECT c.id, c.name, h.candidate FROM companies c "
    "LEFT JOIN LATERAL (SELECT candidate FROM discovery_hits d WHERE d.company_id = c.id "
    "ORDER BY d.created_at LIMIT 1) h ON true "
    "WHERE c.domain_status = 'unresolved' AND NOT EXISTS ("
    "SELECT 1 FROM prescreen_decisions p WHERE p.company_id = c.id) "
    "ORDER BY CASE WHEN :newest THEN c.first_seen_at END DESC, c.first_seen_at LIMIT :limit"
)
# Unresolved companies website intelligence may process. ``required`` = only those with a
# SCRAPE decision; otherwise every company that was not screened out.
_SCRAPE = text(
    "WITH latest AS (SELECT DISTINCT ON (company_id) company_id, decision "
    "FROM prescreen_decisions ORDER BY company_id, created_at DESC, id DESC) "
    "SELECT c.id FROM companies c LEFT JOIN latest l ON l.company_id = c.id "
    "WHERE c.domain_status = 'unresolved' AND ("
    "l.decision = 'scrape' OR (NOT :required AND l.decision IS NULL)) "
    "ORDER BY CASE WHEN :newest THEN c.first_seen_at END DESC, c.first_seen_at LIMIT :limit"
)


class SqlPrescreenStore:
    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def candidates(self, limit: int, *, newest_first: bool = False) -> list[DiscoveryFacts]:
        """``newest_first``: a run that just discovered companies works on those first (so a
        search prompt shows its own results); otherwise the oldest waiting company goes first."""
        with self._sessions() as s:
            rows = list(s.execute(_CANDIDATES, {"limit": limit, "newest": newest_first}))
        out = []
        for company_id, name, candidate in rows:
            data: dict[str, Any] = candidate or {}
            raw: dict[str, Any] = data.get("raw") or {}
            out.append(
                DiscoveryFacts(
                    company_id=UUID(str(company_id)),
                    name=str(name),
                    title=str(data.get("source_title") or ""),
                    description=str(raw.get("description") or ""),
                    source=str(raw.get("discovery_source") or ""),
                    funding_mention=raw.get("funding_mention"),
                )
            )
        return out

    def insert(self, facts: DiscoveryFacts, outcome: PrescreenOutcome, laya_status: str) -> None:
        with self._sessions.begin() as s:
            s.add(
                PrescreenDecisionRow(
                    id=uuid.uuid4(),
                    company_id=facts.company_id,
                    decision=outcome.decision.value,
                    reasons=list(outcome.reasons),
                    laya_status=laya_status,
                    laya_fit=outcome.laya_fit,
                    policy_version=PRESCREEN_POLICY_VERSION,
                )
            )

    def scrape_candidates(
        self, limit: int, *, required: bool, newest_first: bool = False
    ) -> list[UUID]:
        with self._sessions() as s:
            params = {"limit": limit, "required": required, "newest": newest_first}
            rows = s.execute(_SCRAPE, params)
            return [UUID(str(r[0])) for r in rows]
