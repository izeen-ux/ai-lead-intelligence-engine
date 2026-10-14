"""Website-intelligence persistence (Phase 3).

``WebsiteStore`` protocol + PostgreSQL implementation (in-memory version for unit tests).

Merge policy (ADR-029): a company's ``domain`` is set only after the domain was verified
against the homepage. If another company already owns that verified domain, the two rows are
the same company: the newer row is merged INTO the existing one inside one transaction -
discovery hits, website pages, crawls and resolutions are re-pointed (exact duplicates dropped),
counters combined, and a ``company_merges`` row records the audit and acts as an alias so later
discovery of the old name maps to the surviving company. Merges never happen on name
similarity alone.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import Engine, and_, delete, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, aliased, sessionmaker

from app.core.errors import NotFoundError
from app.db.models import (
    CompanyMergeRow,
    CompanyRow,
    DiscoveryHitRow,
    DomainResolutionRow,
    SourceRow,
    WebsiteCrawlRow,
    WebsitePageRow,
)
from app.domain.enums import DomainResolutionStatus
from app.domain.identity import url_hash
from app.domain.models import (
    DomainResolutionRecord,
    WebsiteCrawlRecord,
    compute_content_hash,
    utcnow,
)
from app.domain.website import host_of
from app.providers.contracts import WebPage


@dataclass(frozen=True)
class CompanyContext:
    """What website intelligence needs to know about a candidate company."""

    company_id: UUID
    name: str
    normalized_name: str
    domain: str | None
    domain_status: DomainResolutionStatus
    publisher_hosts: frozenset[str] = frozenset()  # hosts of its discovery sources
    source_titles: tuple[str, ...] = ()


@dataclass(frozen=True)
class AssignOutcome:
    company_id: UUID  # surviving company
    merged_from: UUID | None = None  # company merged into it, if any


class WebsiteStore(Protocol):
    def company_context(self, company_id: UUID) -> CompanyContext: ...

    def companies_needing_resolution(self, limit: int) -> list[UUID]: ...

    def record_resolution(self, record: DomainResolutionRecord) -> None: ...

    def set_domain_status(
        self, company_id: UUID, status: DomainResolutionStatus, confidence: float | None
    ) -> None: ...

    def record_crawl(self, crawl: WebsiteCrawlRecord, pages: Sequence[WebPage]) -> None: ...

    def assign_verified_domain(
        self,
        company_id: UUID,
        domain: str,
        confidence: float,
        source: str,
        evidence: dict[str, Any],
    ) -> AssignOutcome: ...


def _crawl_values(crawl: WebsiteCrawlRecord) -> dict[str, Any]:
    data = crawl.model_dump(mode="json", exclude={"created_at", "updated_at"})
    data["id"] = crawl.id
    data["company_id"] = crawl.company_id
    data["started_at"] = crawl.started_at
    data["finished_at"] = crawl.finished_at
    return data


# ------------------------------------------------------------------------------ PostgreSQL
class SqlWebsiteStore:
    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def company_context(self, company_id: UUID) -> CompanyContext:
        with self._sessions() as s:
            row = s.get(CompanyRow, company_id)
            if row is None:
                raise NotFoundError("company not found", details={"id": str(company_id)})
            sources = s.execute(
                select(SourceRow.url, SourceRow.platform, SourceRow.title)
                .join(DiscoveryHitRow, DiscoveryHitRow.source_id == SourceRow.id)
                .where(DiscoveryHitRow.company_id == company_id)
                .distinct()
            ).all()
            hosts = {host_of(u) for u, _, _ in sources} | {p for _, p, _ in sources if p}
            return CompanyContext(
                company_id=row.id,
                name=row.name,
                normalized_name=row.normalized_name,
                domain=row.domain,
                domain_status=DomainResolutionStatus(row.domain_status),
                publisher_hosts=frozenset(h for h in hosts if h),
                source_titles=tuple(t for _, _, t in sources if t),
            )

    def companies_needing_resolution(self, limit: int) -> list[UUID]:
        with self._sessions() as s:
            return list(
                s.execute(
                    select(CompanyRow.id)
                    .where(CompanyRow.domain_status == DomainResolutionStatus.UNRESOLVED.value)
                    .order_by(CompanyRow.first_seen_at)
                    .limit(limit)
                ).scalars()
            )

    def record_resolution(self, record: DomainResolutionRecord) -> None:
        with self._sessions.begin() as s:
            s.add(
                DomainResolutionRow(
                    id=record.id,
                    company_id=record.company_id,
                    provider=record.provider,
                    query=record.query,
                    status=record.status.value,
                    chosen_domain=record.chosen_domain,
                    confidence=Decimal(str(record.confidence))
                    if record.confidence is not None
                    else None,
                    reason=record.reason,
                    candidates=record.candidates,
                    credits_used=record.credits_used,
                    error_code=record.error_code,
                )
            )

    def set_domain_status(
        self, company_id: UUID, status: DomainResolutionStatus, confidence: float | None
    ) -> None:
        with self._sessions.begin() as s:
            s.execute(
                update(CompanyRow)
                .where(CompanyRow.id == company_id)
                .values(
                    domain_status=status.value,
                    domain_confidence=Decimal(str(confidence)) if confidence is not None else None,
                    updated_at=utcnow(),
                )
            )

    def record_crawl(self, crawl: WebsiteCrawlRecord, pages: Sequence[WebPage]) -> None:
        with self._sessions.begin() as s:
            s.add(WebsiteCrawlRow(**_crawl_values(crawl)))
            s.flush()
            for page in pages:
                url = str(page.url)
                values = {
                    "company_id": crawl.company_id,
                    "crawl_id": crawl.id,
                    "url": url,
                    "url_hash": url_hash(url),
                    "page_type": page.page_type.value,
                    "provider": crawl.provider.value,
                    "status_code": page.status_code,
                    "title": page.title,
                    "description": page.description,
                    "markdown": page.markdown,
                    "content_hash": compute_content_hash(page.markdown),
                    "fetched_at": page.fetched_at,
                }
                stmt = insert(WebsitePageRow).values(id=uuid.uuid4(), **values)
                s.execute(
                    stmt.on_conflict_do_update(
                        constraint="uq_website_pages_company_url",
                        set_={**values, "updated_at": utcnow()},
                    )
                )
            if crawl.pages_succeeded:
                s.execute(
                    update(CompanyRow)
                    .where(CompanyRow.id == crawl.company_id)
                    .values(website_crawled_at=crawl.finished_at or utcnow())
                )

    def assign_verified_domain(
        self,
        company_id: UUID,
        domain: str,
        confidence: float,
        source: str,
        evidence: dict[str, Any],
    ) -> AssignOutcome:
        now = utcnow()
        with self._sessions.begin() as s:
            me = s.get(CompanyRow, company_id, with_for_update=True)
            if me is None:
                raise NotFoundError("company not found", details={"id": str(company_id)})
            owner = s.execute(
                select(CompanyRow)
                .where(CompanyRow.domain == domain, CompanyRow.id != company_id)
                .with_for_update()
            ).scalar_one_or_none()
            if owner is None:
                me.domain = domain
                me.domain_status = DomainResolutionStatus.VERIFIED.value
                me.domain_confidence = Decimal(str(round(confidence, 3)))
                me.domain_source = source
                me.domain_verified_at = now
                me.updated_at = now
                return AssignOutcome(company_id=company_id)
            self._merge(s, source_row=me, into=owner, domain=domain, evidence=evidence)
            return AssignOutcome(company_id=owner.id, merged_from=company_id)

    @staticmethod
    def _merge(
        s: Session,
        *,
        source_row: CompanyRow,
        into: CompanyRow,
        domain: str,
        evidence: dict[str, Any],
    ) -> None:
        src, dst = source_row.id, into.id
        # discovery hits: re-point, dropping exact (run, source) duplicates already on dst
        other = aliased(DiscoveryHitRow)
        dup_hit = exists().where(
            and_(
                other.company_id == dst,
                other.run_id == DiscoveryHitRow.run_id,
                other.source_id == DiscoveryHitRow.source_id,
            )
        )
        s.execute(delete(DiscoveryHitRow).where(DiscoveryHitRow.company_id == src, dup_hit))
        s.execute(
            update(DiscoveryHitRow).where(DiscoveryHitRow.company_id == src).values(company_id=dst)
        )
        # website pages: keep dst's copy of the same URL
        other_page = aliased(WebsitePageRow)
        dup_page = exists().where(
            and_(other_page.company_id == dst, other_page.url_hash == WebsitePageRow.url_hash)
        )
        s.execute(delete(WebsitePageRow).where(WebsitePageRow.company_id == src, dup_page))
        s.execute(
            update(WebsitePageRow).where(WebsitePageRow.company_id == src).values(company_id=dst)
        )
        for table in (WebsiteCrawlRow, DomainResolutionRow):
            s.execute(update(table).where(table.company_id == src).values(company_id=dst))
        # earlier aliases of src now point at dst
        s.execute(
            update(CompanyMergeRow)
            .where(CompanyMergeRow.into_company_id == src)
            .values(into_company_id=dst)
        )
        into.times_seen = into.times_seen + source_row.times_seen
        into.first_seen_at = min(into.first_seen_at, source_row.first_seen_at)
        into.last_seen_at = max(into.last_seen_at, source_row.last_seen_at)
        into.updated_at = utcnow()
        s.add(
            CompanyMergeRow(
                id=uuid.uuid4(),
                from_company_id=src,
                into_company_id=dst,
                from_dedupe_key=source_row.dedupe_key,
                from_name=source_row.name,
                domain=domain,
                reason="same_verified_domain",
                evidence=evidence,
            )
        )
        s.flush()
        s.delete(source_row)

    # -------------------------------------------------------------------- read helpers
    def count_pages(self, company_id: UUID) -> int:
        with self._sessions() as s:
            return int(
                s.execute(
                    select(func.count()).where(WebsitePageRow.company_id == company_id)
                ).scalar_one()
            )


# ------------------------------------------------------------------------------ in-memory
@dataclass
class InMemoryWebsiteStore:
    """Unit-test double with the same observable behaviour (no provenance tables)."""

    contexts: dict[UUID, CompanyContext] = field(default_factory=dict)
    resolutions: list[DomainResolutionRecord] = field(default_factory=list)
    crawls: list[WebsiteCrawlRecord] = field(default_factory=list)
    pages: dict[UUID, list[WebPage]] = field(default_factory=dict)
    statuses: dict[UUID, tuple[DomainResolutionStatus, float | None]] = field(default_factory=dict)
    domains: dict[str, UUID] = field(default_factory=dict)
    merges: list[tuple[UUID, UUID, str]] = field(default_factory=list)

    def add_company(self, context: CompanyContext) -> None:
        self.contexts[context.company_id] = context

    def company_context(self, company_id: UUID) -> CompanyContext:
        try:
            return self.contexts[company_id]
        except KeyError:
            raise NotFoundError("company not found", details={"id": str(company_id)}) from None

    def companies_needing_resolution(self, limit: int) -> list[UUID]:
        pending = [
            c.company_id
            for c in self.contexts.values()
            if self.statuses.get(c.company_id, (c.domain_status, None))[0]
            is DomainResolutionStatus.UNRESOLVED
        ]
        return pending[:limit]

    def record_resolution(self, record: DomainResolutionRecord) -> None:
        self.resolutions.append(record)

    def set_domain_status(
        self, company_id: UUID, status: DomainResolutionStatus, confidence: float | None
    ) -> None:
        self.statuses[company_id] = (status, confidence)

    def record_crawl(self, crawl: WebsiteCrawlRecord, pages: Sequence[WebPage]) -> None:
        self.crawls.append(crawl)
        self.pages.setdefault(crawl.company_id, []).extend(pages)

    def assign_verified_domain(
        self,
        company_id: UUID,
        domain: str,
        confidence: float,
        source: str,
        evidence: dict[str, Any],
    ) -> AssignOutcome:
        owner = self.domains.get(domain)
        if owner is not None and owner != company_id:
            self.merges.append((company_id, owner, domain))
            self.pages.setdefault(owner, []).extend(self.pages.pop(company_id, []))
            self.contexts.pop(company_id, None)
            return AssignOutcome(company_id=owner, merged_from=company_id)
        self.domains[domain] = company_id
        self.statuses[company_id] = (DomainResolutionStatus.VERIFIED, confidence)
        return AssignOutcome(company_id=company_id)
