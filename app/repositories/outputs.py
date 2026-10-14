"""Phase 8 persistence + read models: export history, human-review audit, pipeline counts and
stored usage/cost. Export items and human reviews are INSERT-ONLY (triggers, migration 0008).

Nothing here writes qualification, enrichment or analysis history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import Engine, select, text
from sqlalchemy.orm import sessionmaker

from app.core.errors import NotFoundError
from app.db.models import ExportItemRow, ExportRunRow, HumanReviewRow
from app.domain.analysis.records import AnalysisStatus
from app.domain.enums import ContactStatus, QualificationDecision, RunStatus
from app.domain.outputs.leads import lead_id_for
from app.domain.outputs.records import (
    ExportItemRecord,
    ExportMode,
    ExportRunRecord,
    HumanReviewRecord,
    ReviewAction,
    ReviewOutcome,
)
from app.domain.outputs.sync import RowAction
from app.repositories.analysis import InMemoryAnalysisStore
from app.repositories.enrichment import InMemoryEnrichmentStore
from app.repositories.qualification import InMemoryQualificationStore

PRESENT = (RowAction.INSERT, RowAction.UPDATE, RowAction.UNCHANGED)


@dataclass(frozen=True)
class PipelineCounts:
    discovered: int = 0
    verified: int = 0
    qualified: int = 0
    review: int = 0
    not_qualified: int = 0
    enriched: int = 0  # companies with a VERIFIED contact
    analyzed: int = 0  # companies with a SUCCEEDED analysis
    exported: int = 0  # leads currently present in the live Leads worksheet


@dataclass(frozen=True)
class UsageLine:
    provider: str
    metric: str
    all_time: Decimal
    this_month: Decimal
    basis: str  # "provider_reported" (value returned by the provider) | "ledger_estimate"
    note: str = ""


class OutputStore(Protocol):
    def company_ids_by_decision(
        self, decision: QualificationDecision, limit: int, offset: int = 0
    ) -> list[UUID]: ...

    def all_company_ids(self, limit: int, offset: int = 0) -> list[UUID]: ...

    def resolve_lead_ids(self, lead_ids: list[str]) -> dict[str, UUID]: ...

    def create_export_run(self, run: ExportRunRecord) -> None: ...

    def finish_export_run(self, run: ExportRunRecord) -> None: ...

    def insert_export_items(self, items: list[ExportItemRecord]) -> None: ...

    def export_runs(self, limit: int) -> list[ExportRunRecord]: ...

    def export_items(self, run_id: UUID) -> list[ExportItemRecord]: ...

    def last_export_item(self, lead_id: str) -> ExportItemRecord | None: ...

    def insert_human_review(self, record: HumanReviewRecord) -> None: ...

    def human_reviews(self, company_id: UUID) -> list[HumanReviewRecord]: ...

    def pipeline_counts(self) -> PipelineCounts: ...

    def usage_summary(self, month_start: datetime) -> list[UsageLine]: ...

    def screening_stats(self) -> dict[str, Any]: ...


# ------------------------------------------------------------------------------ SQL
# Latest decision per company (same ordering as SqlQualificationStore.latest_decision).
_IDS_BY_LATEST_DECISION = text(
    "WITH latest AS (SELECT DISTINCT ON (company_id) company_id, decision, decided_at "
    "FROM qualification_decisions ORDER BY company_id, decided_at DESC, id DESC) "
    "SELECT company_id FROM latest WHERE decision = :d "
    "ORDER BY decided_at DESC, company_id LIMIT :limit OFFSET :offset"
)


def _run_values(run: ExportRunRecord) -> dict[str, Any]:
    data = run.model_dump(mode="json", exclude={"created_at", "updated_at"})
    data.update(id=run.id, started_at=run.started_at, finished_at=run.finished_at)
    return data


def _run_record(r: ExportRunRow) -> ExportRunRecord:
    return ExportRunRecord(
        id=r.id,
        status=RunStatus(r.status),
        mode=ExportMode(r.mode),
        target=r.target,
        spreadsheet_ref=r.spreadsheet_ref,
        worksheets=[str(w) for w in r.worksheets],
        config=dict(r.config),
        leads_considered=r.leads_considered,
        inserted_count=r.inserted_count,
        updated_count=r.updated_count,
        unchanged_count=r.unchanged_count,
        withdrawn_count=r.withdrawn_count,
        skipped_count=r.skipped_count,
        failed_count=r.failed_count,
        duplicate_rows_found=r.duplicate_rows_found,
        api_calls=r.api_calls,
        error_code=r.error_code,
        error_message=r.error_message,
        started_at=r.started_at,
        finished_at=r.finished_at,
    )


def _item_record(r: ExportItemRow) -> ExportItemRecord:
    return ExportItemRecord(
        id=r.id,
        created_at=r.created_at,
        updated_at=r.created_at,
        run_id=r.run_id,
        worksheet=r.worksheet,
        lead_id=r.lead_id,
        company_id=r.company_id,
        qualification_id=r.qualification_id,
        contact_id=r.contact_id,
        analysis_id=r.analysis_id,
        action=RowAction(r.action),
        row_number=r.row_number,
        row_hash=r.row_hash,
        export_status=r.export_status,
        error=r.error,
    )


def _review_record(r: HumanReviewRow) -> HumanReviewRecord:
    return HumanReviewRecord(
        id=r.id,
        created_at=r.created_at,
        updated_at=r.created_at,
        company_id=r.company_id,
        action=ReviewAction(r.action),
        outcome=ReviewOutcome(r.outcome),
        reviewer=r.reviewer,
        reason=r.reason,
        prior_decision_id=r.prior_decision_id,
        resulting_decision_id=r.resulting_decision_id,
        hard_rules_passed=r.hard_rules_passed,
        hard_rule_results=list(r.hard_rule_results),
        refusal_code=r.refusal_code,
        policy_version=r.policy_version,
        reviewed_at=r.reviewed_at,
    )


def ai_provider_label(gateway: str) -> str:
    """Name shown for AI usage. Analyses store the gateway that answered: "openai" (Phase 10),
    "mock", or a Techno-AI-Router gateway such as "openrouter" (historical, unchanged)."""
    if gateway in ("openai", "anthropic"):
        return gateway
    if gateway == "mock":
        return "mock_ai"
    return f"ai_router ({gateway})"


def _d(value: object) -> Decimal:
    return Decimal(str(value or 0))


class SqlOutputStore:
    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def company_ids_by_decision(
        self, decision: QualificationDecision, limit: int, offset: int = 0
    ) -> list[UUID]:
        with self._sessions() as s:
            rows = s.execute(
                _IDS_BY_LATEST_DECISION, {"d": decision.value, "limit": limit, "offset": offset}
            )
            return [UUID(str(r[0])) for r in rows]

    def all_company_ids(self, limit: int, offset: int = 0) -> list[UUID]:
        sql = text("SELECT id FROM companies ORDER BY name, id LIMIT :limit OFFSET :offset")
        with self._sessions() as s:
            return [UUID(str(r[0])) for r in s.execute(sql, {"limit": limit, "offset": offset})]

    def resolve_lead_ids(self, lead_ids: list[str]) -> dict[str, UUID]:
        wanted = set(lead_ids)
        if not wanted:
            return {}
        with self._sessions() as s:
            ids = [UUID(str(r[0])) for r in s.execute(text("SELECT id FROM companies"))]
        return {lead_id_for(i): i for i in ids if lead_id_for(i) in wanted}

    def create_export_run(self, run: ExportRunRecord) -> None:
        with self._sessions.begin() as s:
            s.add(ExportRunRow(**_run_values(run)))

    def finish_export_run(self, run: ExportRunRecord) -> None:
        with self._sessions.begin() as s:
            row = s.get(ExportRunRow, run.id)
            if row is None:
                raise NotFoundError("export run not found", details={"id": str(run.id)})
            for key, value in _run_values(run).items():
                setattr(row, key, value)

    def insert_export_items(self, items: list[ExportItemRecord]) -> None:
        if not items:
            return
        with self._sessions.begin() as s:
            for item in items:
                data = item.model_dump(mode="json", exclude={"created_at", "updated_at"})
                data.update(
                    id=item.id,
                    run_id=item.run_id,
                    company_id=item.company_id,
                    qualification_id=item.qualification_id,
                    contact_id=item.contact_id,
                    analysis_id=item.analysis_id,
                )
                s.add(ExportItemRow(**data))

    def export_runs(self, limit: int) -> list[ExportRunRecord]:
        with self._sessions() as s:
            rows = s.scalars(
                select(ExportRunRow).order_by(ExportRunRow.started_at.desc()).limit(limit)
            )
            return [_run_record(r) for r in rows]

    def export_items(self, run_id: UUID) -> list[ExportItemRecord]:
        with self._sessions() as s:
            rows = s.scalars(
                select(ExportItemRow)
                .where(ExportItemRow.run_id == run_id)
                .order_by(ExportItemRow.worksheet, ExportItemRow.row_number)
            )
            return [_item_record(r) for r in rows]

    def last_export_item(self, lead_id: str) -> ExportItemRecord | None:
        with self._sessions() as s:
            row = s.scalars(
                select(ExportItemRow)
                .join(ExportRunRow, ExportRunRow.id == ExportItemRow.run_id)
                .where(ExportItemRow.lead_id == lead_id, ExportRunRow.mode == "live")
                .order_by(ExportItemRow.created_at.desc())
                .limit(1)
            ).first()
            return _item_record(row) if row is not None else None

    def insert_human_review(self, record: HumanReviewRecord) -> None:
        data = record.model_dump(mode="json", exclude={"created_at", "updated_at"})
        data.update(
            id=record.id,
            company_id=record.company_id,
            prior_decision_id=record.prior_decision_id,
            resulting_decision_id=record.resulting_decision_id,
            reviewed_at=record.reviewed_at,
        )
        with self._sessions.begin() as s:
            s.add(HumanReviewRow(**data))

    def human_reviews(self, company_id: UUID) -> list[HumanReviewRecord]:
        with self._sessions() as s:
            rows = s.scalars(
                select(HumanReviewRow)
                .where(HumanReviewRow.company_id == company_id)
                .order_by(HumanReviewRow.reviewed_at.desc())
            )
            return [_review_record(r) for r in rows]

    def pipeline_counts(self) -> PipelineCounts:
        sql = text(
            "WITH latest AS (SELECT DISTINCT ON (company_id) company_id, decision "
            "FROM qualification_decisions ORDER BY company_id, decided_at DESC, id DESC) "
            "SELECT "
            "(SELECT count(*) FROM companies), "
            "(SELECT count(*) FROM companies WHERE domain_status = 'verified'), "
            "(SELECT count(*) FROM latest WHERE decision = 'qualified'), "
            "(SELECT count(*) FROM latest WHERE decision = 'review'), "
            "(SELECT count(*) FROM latest WHERE decision = 'not_qualified'), "
            "(SELECT count(DISTINCT company_id) FROM enrichment_contacts "
            " WHERE status = 'verified'), "
            "(SELECT count(DISTINCT company_id) FROM lead_analyses WHERE status = 'succeeded'), "
            "(SELECT count(*) FROM (SELECT DISTINCT ON (i.lead_id) i.lead_id, i.action "
            "  FROM export_items i JOIN export_runs r ON r.id = i.run_id "
            "  WHERE r.mode = 'live' AND i.worksheet = r.worksheets->>0 "
            "  AND i.company_id IS NOT NULL "
            "  AND i.action IN ('insert','update','unchanged','withdraw') "
            "  ORDER BY i.lead_id, i.created_at DESC) last WHERE last.action <> 'withdraw')"
        )
        with self._sessions() as s:
            row = s.execute(sql).one()
        return PipelineCounts(*(int(v or 0) for v in row))

    def usage_summary(self, month_start: datetime) -> list[UsageLine]:
        m = {"m": month_start}
        q = {
            "apify_cost": "SELECT coalesce(sum(cost_usd),0), coalesce(sum(cost_usd) "
            "FILTER (WHERE created_at >= :m),0) FROM discovery_runs",
            "firecrawl": "SELECT coalesce(sum(c),0), coalesce(sum(c) FILTER (WHERE t >= :m),0) "
            "FROM (SELECT credits_used c, started_at t FROM website_crawls UNION ALL "
            "SELECT credits_used, created_at FROM domain_resolutions) x",
        }
        with self._sessions() as s:
            got = {k: s.execute(text(v), m).one() for k, v in q.items()}
            credits = s.execute(
                text(
                    "SELECT provider, coalesce(sum(credits),0), coalesce(sum(credits) FILTER "
                    "(WHERE created_at >= :m),0), coalesce(sum(estimated_cost_usd),0), "
                    "coalesce(sum(estimated_cost_usd) FILTER (WHERE created_at >= :m),0) "
                    "FROM enrichment_attempts GROUP BY provider ORDER BY provider"
                ),
                m,
            ).all()
            ai = s.execute(
                text(
                    "SELECT coalesce(gateway, 'unknown'), "
                    "coalesce(sum((usage->>'total_tokens')::int),0), "
                    "coalesce(sum((usage->>'total_tokens')::int) "
                    "FILTER (WHERE analyzed_at >= :m),0), coalesce(sum(cost_usd),0), "
                    "coalesce(sum(cost_usd) FILTER (WHERE analyzed_at >= :m),0) "
                    "FROM lead_analyses WHERE usage <> '{}'::jsonb GROUP BY 1 ORDER BY 1"
                ),
                m,
            ).all()
        lines = [
            UsageLine("apify", "usd", _d(got["apify_cost"][0]), _d(got["apify_cost"][1]),
                      "provider_reported", "Apify run usage recorded per discovery run"),
            UsageLine("firecrawl", "credits", _d(got["firecrawl"][0]), _d(got["firecrawl"][1]),
                      "provider_reported", "credits reported by Firecrawl per call"),
        ]  # fmt: skip
        for provider, c_all, c_month, usd_all, usd_month in credits:
            lines.append(UsageLine(str(provider), "credits", _d(c_all), _d(c_month),
                                   "ledger_estimate", "app ledger; the provider account "
                                   "counter is authoritative"))  # fmt: skip
            lines.append(UsageLine(str(provider), "usd", _d(usd_all), _d(usd_month),
                                   "ledger_estimate"))  # fmt: skip
        for gateway, t_all, t_month, usd_all, usd_month in ai:
            provider = ai_provider_label(str(gateway))
            note = "token usage returned by the provider"
            lines.append(UsageLine(provider, "tokens", _d(t_all), _d(t_month),
                                   "provider_reported", note))  # fmt: skip
            lines.append(UsageLine(provider, "usd", _d(usd_all), _d(usd_month),
                                   "ledger_estimate", "tokens x configured price"))  # fmt: skip
        return lines

    def screening_stats(self) -> dict[str, Any]:
        """How much paid scraping the free pre-screen avoided: companies screened / skipped,
        the average website credits one scraped company costs, and the resulting estimate."""
        sql = text(
            "SELECT (SELECT count(*) FROM prescreen_decisions), "
            "(SELECT count(*) FROM prescreen_decisions WHERE decision = 'skip'), "
            "(SELECT count(*) FROM prescreen_decisions WHERE laya_status = 'ok'), "
            "(SELECT count(DISTINCT company_id) FROM website_crawls), "
            "(SELECT coalesce(sum(c),0) FROM (SELECT credits_used c FROM website_crawls "
            "UNION ALL SELECT credits_used FROM domain_resolutions) x)"
        )
        with self._sessions() as s:
            screened, skipped, laya, scraped, credits = s.execute(sql).one()
        average = (Decimal(credits) / Decimal(scraped)) if scraped else Decimal(0)
        return {
            "screened": int(screened),
            "skipped": int(skipped),
            "checked_by_laya": int(laya),
            "companies_scraped": int(scraped),
            "website_credits_spent": str(_d(credits)),
            "avg_credits_per_scraped_company": f"{average:.1f}",
            "estimated_credits_saved": f"{average * int(skipped):.0f}",
        }


# ------------------------------------------------------------------------------ in-memory
@dataclass
class InMemoryOutputStore:
    """Demo/test store over the in-memory Phase 5/6/7 stores."""

    qualifications: InMemoryQualificationStore
    enrichment: InMemoryEnrichmentStore
    analyses: InMemoryAnalysisStore
    runs: dict[UUID, ExportRunRecord] = field(default_factory=dict)
    items: list[ExportItemRecord] = field(default_factory=list)
    reviews: list[HumanReviewRecord] = field(default_factory=list)

    def _latest(self) -> list[tuple[UUID, QualificationDecision, datetime]]:
        out = []
        for cid in {d.company_id for d in self.qualifications.decisions}:
            d = self.qualifications.latest_decision(cid)
            if d is not None:
                out.append((cid, d.decision, d.decided_at))
        return sorted(out, key=lambda x: (x[2], str(x[0])), reverse=True)

    def company_ids_by_decision(
        self, decision: QualificationDecision, limit: int, offset: int = 0
    ) -> list[UUID]:
        ids = [cid for cid, dec, _ in self._latest() if dec is decision]
        return ids[offset : offset + limit]

    def all_company_ids(self, limit: int, offset: int = 0) -> list[UUID]:
        ordered = sorted(
            self.enrichment.companies.values(), key=lambda c: (c.name, str(c.company_id))
        )
        return [c.company_id for c in ordered][offset : offset + limit]

    def resolve_lead_ids(self, lead_ids: list[str]) -> dict[str, UUID]:
        wanted = set(lead_ids)
        return {lead_id_for(c): c for c in self.enrichment.companies if lead_id_for(c) in wanted}

    def create_export_run(self, run: ExportRunRecord) -> None:
        self.runs[run.id] = run.model_copy(deep=True)

    def finish_export_run(self, run: ExportRunRecord) -> None:
        self.runs[run.id] = run.model_copy(deep=True)

    def insert_export_items(self, items: list[ExportItemRecord]) -> None:
        known = {i.id for i in self.items}
        if any(i.id in known for i in items):
            raise ValueError("export items are append-only")
        self.items.extend(i.model_copy(deep=True) for i in items)

    def export_runs(self, limit: int) -> list[ExportRunRecord]:
        return sorted(self.runs.values(), key=lambda r: r.started_at, reverse=True)[:limit]

    def export_items(self, run_id: UUID) -> list[ExportItemRecord]:
        return [i for i in self.items if i.run_id == run_id]

    def last_export_item(self, lead_id: str) -> ExportItemRecord | None:
        live = {r.id for r in self.runs.values() if r.mode.value == "live"}
        found = [i for i in self.items if i.lead_id == lead_id and i.run_id in live]
        return found[-1] if found else None

    def insert_human_review(self, record: HumanReviewRecord) -> None:
        if any(r.id == record.id for r in self.reviews):
            raise ValueError("human reviews are append-only")
        self.reviews.append(record.model_copy(deep=True))

    def human_reviews(self, company_id: UUID) -> list[HumanReviewRecord]:
        found = [r for r in self.reviews if r.company_id == company_id]
        return sorted(found, key=lambda r: r.reviewed_at, reverse=True)

    def pipeline_counts(self) -> PipelineCounts:
        latest = [dec for _, dec, _ in self._latest()]
        live = {r.id: r for r in self.runs.values() if r.mode.value == "live"}
        last: dict[str, RowAction] = {}
        for item in self.items:
            run = live.get(item.run_id)
            in_leads_tab = (
                run is not None and item.worksheet == run.worksheets[0] and item.company_id
            )
            if in_leads_tab and item.action in (*PRESENT, RowAction.WITHDRAW):
                last[item.lead_id] = item.action
        enriched = {
            c.company_id for c in self.enrichment.contact_log if c.status is ContactStatus.VERIFIED
        }
        analyzed = {
            r.company_id for r in self.analyses.records if r.status is AnalysisStatus.SUCCEEDED
        }
        return PipelineCounts(
            discovered=len(self.enrichment.companies),
            verified=sum(
                1 for c in self.enrichment.companies.values() if c.domain_status.value == "verified"
            ),
            qualified=latest.count(QualificationDecision.QUALIFIED),
            review=latest.count(QualificationDecision.REVIEW),
            not_qualified=latest.count(QualificationDecision.NOT_QUALIFIED),
            enriched=len(enriched),
            analyzed=len(analyzed),
            exported=sum(1 for a in last.values() if a is not RowAction.WITHDRAW),
        )

    def screening_stats(self) -> dict[str, Any]:
        return {
            "screened": 0, "skipped": 0, "checked_by_laya": 0, "companies_scraped": 0,
            "website_credits_spent": "0", "avg_credits_per_scraped_company": "0.0",
            "estimated_credits_saved": "0",
        }  # fmt: skip

    def usage_summary(self, month_start: datetime) -> list[UsageLine]:
        lines: list[UsageLine] = []
        by: dict[str, list[Decimal]] = {}
        for a in self.enrichment.attempt_log:
            acc = by.setdefault(a.provider.value, [Decimal(0)] * 4)
            month = a.created_at >= month_start
            acc[0] += a.credits
            acc[1] += a.credits if month else 0
            acc[2] += a.estimated_cost_usd
            acc[3] += a.estimated_cost_usd if month else 0
        for provider, (c_all, c_month, u_all, u_month) in sorted(by.items()):
            lines.append(UsageLine(provider, "credits", c_all, c_month, "ledger_estimate",
                                   "app ledger; the provider account counter is "
                                   "authoritative"))  # fmt: skip
            lines.append(UsageLine(provider, "usd", u_all, u_month, "ledger_estimate"))
        ai: dict[str, list[Decimal]] = {}
        for r in self.analyses.records:
            if not r.usage:
                continue
            acc = ai.setdefault(ai_provider_label(r.gateway or "unknown"), [Decimal(0)] * 2)
            used = Decimal(r.usage.get("total_tokens", 0))
            acc[0] += used
            acc[1] += used if r.analyzed_at >= month_start else 0
        for provider, (t_all, t_month) in sorted(ai.items()):
            lines.append(UsageLine(provider, "tokens", t_all, t_month, "provider_reported",
                                   "token usage returned by the provider"))  # fmt: skip
        return lines
