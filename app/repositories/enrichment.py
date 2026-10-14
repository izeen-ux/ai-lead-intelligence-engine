"""Enrichment persistence (Phase 6).

* Reads: QUALIFIED candidates (latest decision per company), company + its crawled pages
  (identity sources), cache entries, month-to-date credits per provider (budgets).
* Writes: runs, INSERT-ONLY contacts and provider attempts (PostgreSQL triggers from migration
  0006 reject UPDATE), and the mutable cache (upsert).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import Engine, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import NotFoundError
from app.db.models import (
    CompanyRow,
    DiscoveryHitRow,
    EnrichmentAttemptRow,
    EnrichmentCacheRow,
    EnrichmentContactRow,
    EnrichmentRunRow,
    LeadOwnerRow,
    WebsiteCrawlRow,
    WebsitePageRow,
)
from app.domain.enrichment.identity import PageText
from app.domain.enrichment.records import (
    CacheEntry,
    EnrichmentContactRecord,
    EnrichmentRunRecord,
    ProviderAttemptRecord,
)
from app.domain.enums import (
    AttemptStatus,
    ContactStatus,
    DomainResolutionStatus,
    EmailVerificationStatus,
    PhoneStatus,
    ProviderName,
    ProviderOperation,
)


@dataclass(frozen=True)
class CompanyContext:
    company_id: UUID
    name: str
    domain: str | None
    domain_status: DomainResolutionStatus
    pages: tuple[PageText, ...] = ()
    # Stored company facts used only for the lead view (never sent to a provider).
    location: str | None = None
    country_code: str | None = None
    industry: str | None = None
    description: str | None = None  # the company's own homepage title + description
    social_links: Mapping[str, str] = field(default_factory=dict)  # website link metadata
    funding_round: str | None = None  # round named by the discovery headline
    owner: str | None = None  # team member whose run produced this lead


class EnrichmentStore(Protocol):
    def qualified_company_ids(self, limit: int) -> list[UUID]: ...

    def load_company(self, company_id: UUID) -> CompanyContext: ...

    def create_run(self, run: EnrichmentRunRecord) -> None: ...

    def finish_run(self, run: EnrichmentRunRecord) -> None: ...

    def insert_contact(self, contact: EnrichmentContactRecord) -> None: ...

    def insert_attempt(self, attempt: ProviderAttemptRecord) -> None: ...

    def cache_get(self, key: str) -> CacheEntry | None: ...

    def cache_put(self, entry: CacheEntry) -> None: ...

    def cache_hit(self, key: str) -> None: ...

    def month_credits(self, since: datetime) -> dict[ProviderName, Decimal]: ...

    def contacts(self, company_id: UUID) -> list[EnrichmentContactRecord]: ...


def _dec(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(round(value, 3)))


def _flt(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _run_values(run: EnrichmentRunRecord) -> dict[str, Any]:
    data = run.model_dump(mode="json", exclude={"created_at", "updated_at"})
    data.update(
        id=run.id,
        started_at=run.started_at,
        finished_at=run.finished_at,
        hunter_credits=run.hunter_credits,
        apollo_credits=run.apollo_credits,
        estimated_cost_usd=run.estimated_cost_usd,
    )
    return data


def _contact_row(c: EnrichmentContactRecord) -> EnrichmentContactRow:
    data = c.model_dump(mode="json", exclude={"created_at", "updated_at", "company_name", "domain"})
    data.update(
        id=c.id,
        run_id=c.run_id,
        company_id=c.company_id,
        qualification_id=c.qualification_id,
        verified_at=c.verified_at,
        confidence=_dec(c.confidence),
        estimated_cost_usd=c.estimated_cost_usd,
    )
    return EnrichmentContactRow(**data)


def _contact_record(
    r: EnrichmentContactRow, name: str, domain: str | None
) -> EnrichmentContactRecord:
    return EnrichmentContactRecord(
        id=r.id,
        created_at=r.created_at,
        updated_at=r.created_at,
        run_id=r.run_id,
        company_id=r.company_id,
        company_name=name,
        domain=domain,
        qualification_id=r.qualification_id,
        person_key=r.person_key,
        full_name=r.full_name,
        first_name=r.first_name,
        last_name=r.last_name,
        title=r.title,
        role=r.role,
        role_rank=r.role_rank,
        linkedin_url=r.linkedin_url,
        phone=r.phone,
        phone_status=PhoneStatus(r.phone_status),
        email=r.email,
        email_verification=EmailVerificationStatus(r.email_verification),
        verification_provider=ProviderName(r.verification_provider)
        if r.verification_provider
        else None,
        verified_at=r.verified_at,
        confidence=_flt(r.confidence),
        status=ContactStatus(r.status),
        provider=ProviderName(r.provider) if r.provider else None,
        from_cache=r.from_cache,
        identity=dict(r.identity),
        provenance=dict(r.provenance),
        reasons=[str(x) for x in r.reasons],
        credits_used={str(k): str(v) for k, v in r.credits_used.items()},
        estimated_cost_usd=r.estimated_cost_usd,
        policy_version=r.policy_version,
    )


def _cache_record(r: EnrichmentCacheRow) -> CacheEntry:
    return CacheEntry(
        cache_key=r.cache_key,
        company_id=r.company_id,
        person_key=r.person_key,
        domain=r.domain,
        email=r.email,
        email_verification=EmailVerificationStatus(r.email_verification),
        verified_at=r.verified_at,
        confidence=_flt(r.confidence),
        provider=ProviderName(r.provider) if r.provider else None,
        title=r.title,
        linkedin_url=r.linkedin_url,
        phone=r.phone,
        phone_status=PhoneStatus(r.phone_status),
        provenance=dict(r.provenance),
        cached_at=r.cached_at,
        expires_at=r.expires_at,
    )


# ------------------------------------------------------------------------------ PostgreSQL
class SqlEnrichmentStore:
    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def qualified_company_ids(self, limit: int) -> list[UUID]:
        # latest decision per company (DISTINCT ON) that is QUALIFIED; the service re-checks
        # every id through ensure_enrichment_allowed anyway (defence in depth).
        sql = text(
            "SELECT company_id FROM ("
            "  SELECT DISTINCT ON (company_id) company_id, decision, decided_at"
            "  FROM qualification_decisions ORDER BY company_id, decided_at DESC, id DESC"
            ") latest WHERE decision = 'qualified' ORDER BY decided_at DESC LIMIT :limit"
        )
        with self._sessions() as s:
            return [UUID(str(r[0])) for r in s.execute(sql, {"limit": limit})]

    def load_company(self, company_id: UUID) -> CompanyContext:
        with self._sessions() as s:
            row = s.get(CompanyRow, company_id)
            if row is None:
                raise NotFoundError("company not found", details={"id": str(company_id)})
            pages = s.execute(
                select(WebsitePageRow)
                .where(WebsitePageRow.company_id == company_id)
                .order_by(WebsitePageRow.page_type, WebsitePageRow.url)
            ).scalars()
            return CompanyContext(
                company_id=row.id,
                name=row.name,
                domain=row.domain,
                domain_status=DomainResolutionStatus(row.domain_status),
                pages=tuple(PageText(url=p.url, markdown=p.markdown, page_id=p.id) for p in pages),
                location=row.location,
                country_code=row.country_code,
                industry=row.industry,
                owner=s.execute(
                    select(LeadOwnerRow.owner).where(LeadOwnerRow.company_id == company_id)
                ).scalar(),
                **self._site_facts(s, company_id),
            )

    @staticmethod
    def _site_facts(s: Session, company_id: UUID) -> dict[str, Any]:
        """Homepage description + social links of the newest crawls, and the funding round
        the discovery headline named. Read-only; missing data stays missing."""
        crawls = list(
            s.execute(
                select(WebsiteCrawlRow.facts, WebsiteCrawlRow.social_links)
                .where(WebsiteCrawlRow.company_id == company_id)
                .order_by(WebsiteCrawlRow.started_at.desc())
                .limit(10)
            )
        )
        facts: dict[str, Any] = next((f for f, _ in crawls if f), {})
        links: dict[str, str] = {}
        for _, social in reversed(crawls):  # newest crawl wins per platform
            links.update({str(k): str(v) for k, v in (social or {}).items()})
        description = " | ".join(
            str(facts[k]) for k in ("home_title", "home_description") if facts.get(k)
        )
        hit = s.execute(
            select(DiscoveryHitRow.candidate)
            .where(DiscoveryHitRow.company_id == company_id)
            .order_by(DiscoveryHitRow.created_at.desc())
            .limit(1)
        ).scalar()
        raw = (hit or {}).get("raw") or {}
        return {
            "description": description or None,
            "social_links": links,
            "funding_round": raw.get("funding_round"),
        }

    def create_run(self, run: EnrichmentRunRecord) -> None:
        with self._sessions.begin() as s:
            s.add(EnrichmentRunRow(**_run_values(run)))

    def finish_run(self, run: EnrichmentRunRecord) -> None:
        with self._sessions.begin() as s:
            row = s.get(EnrichmentRunRow, run.id)
            if row is None:
                raise NotFoundError("enrichment run not found", details={"id": str(run.id)})
            for key, value in _run_values(run).items():
                setattr(row, key, value)

    def insert_contact(self, contact: EnrichmentContactRecord) -> None:
        with self._sessions.begin() as s:
            s.add(_contact_row(contact))

    def insert_attempt(self, attempt: ProviderAttemptRecord) -> None:
        data = attempt.model_dump(mode="json", exclude={"created_at", "updated_at"})
        data.update(
            id=attempt.id,
            run_id=attempt.run_id,
            company_id=attempt.company_id,
            credits=attempt.credits,
            estimated_cost_usd=attempt.estimated_cost_usd,
        )
        with self._sessions.begin() as s:
            s.add(EnrichmentAttemptRow(**data))

    def cache_get(self, key: str) -> CacheEntry | None:
        with self._sessions() as s:
            row = s.get(EnrichmentCacheRow, key)
            return _cache_record(row) if row is not None else None

    def cache_put(self, entry: CacheEntry) -> None:
        values: dict[str, Any] = {
            "cache_key": entry.cache_key,
            "company_id": entry.company_id,
            "person_key": entry.person_key,
            "domain": entry.domain,
            "email": entry.email,
            "email_verification": entry.email_verification.value,
            "verified_at": entry.verified_at,
            "confidence": _dec(entry.confidence),
            "provider": entry.provider.value if entry.provider else None,
            "title": entry.title,
            "linkedin_url": entry.linkedin_url,
            "phone": entry.phone,
            "phone_status": entry.phone_status.value,
            "provenance": entry.provenance,
            "cached_at": entry.cached_at,
            "expires_at": entry.expires_at,
        }
        stmt = insert(EnrichmentCacheRow).values(**values)
        update = {k: stmt.excluded[k] for k in values if k != "cache_key"}  # hits kept
        with self._sessions.begin() as s:
            s.execute(stmt.on_conflict_do_update(index_elements=["cache_key"], set_=update))

    def cache_hit(self, key: str) -> None:
        with self._sessions.begin() as s:
            row = s.get(EnrichmentCacheRow, key)
            if row is not None:
                row.hits += 1

    def month_credits(self, since: datetime) -> dict[ProviderName, Decimal]:
        with self._sessions() as s:
            rows = s.execute(
                select(
                    EnrichmentAttemptRow.provider,
                    func.coalesce(func.sum(EnrichmentAttemptRow.credits), 0),
                )
                .where(EnrichmentAttemptRow.created_at >= since)
                .group_by(EnrichmentAttemptRow.provider)
            ).all()
        out: dict[ProviderName, Decimal] = {}
        for provider, credits in rows:
            if provider in (ProviderName.HUNTER.value, ProviderName.APOLLO.value):
                out[ProviderName(provider)] = Decimal(credits)
        return out

    def contacts(self, company_id: UUID) -> list[EnrichmentContactRecord]:
        with self._sessions() as s:
            rows = s.execute(
                select(EnrichmentContactRow, CompanyRow.name, CompanyRow.domain)
                .join(CompanyRow, CompanyRow.id == EnrichmentContactRow.company_id)
                .where(EnrichmentContactRow.company_id == company_id)
                .order_by(EnrichmentContactRow.created_at.desc(), EnrichmentContactRow.id)
            ).tuples()
            return [_contact_record(r, name, domain) for r, name, domain in rows]

    def attempts(self, run_id: UUID) -> list[ProviderAttemptRecord]:
        with self._sessions() as s:
            rows = s.execute(
                select(EnrichmentAttemptRow)
                .where(EnrichmentAttemptRow.run_id == run_id)
                .order_by(EnrichmentAttemptRow.created_at, EnrichmentAttemptRow.id)
            ).scalars()
            return [
                ProviderAttemptRecord(
                    id=r.id,
                    run_id=r.run_id,
                    company_id=r.company_id,
                    person_key=r.person_key,
                    provider=ProviderName(r.provider),
                    operation=ProviderOperation(r.operation),
                    status=AttemptStatus(r.status),
                    error_code=r.error_code,
                    http_status=r.http_status,
                    latency_ms=r.latency_ms,
                    credits=r.credits,
                    estimated_cost_usd=r.estimated_cost_usd,
                    detail=dict(r.detail),
                )
                for r in rows
            ]


# ------------------------------------------------------------------------------ in-memory
@dataclass
class InMemoryEnrichmentStore:
    companies: dict[UUID, CompanyContext] = field(default_factory=dict)
    qualified: list[UUID] = field(default_factory=list)
    runs: dict[UUID, EnrichmentRunRecord] = field(default_factory=dict)
    contact_log: list[EnrichmentContactRecord] = field(default_factory=list)
    attempt_log: list[ProviderAttemptRecord] = field(default_factory=list)
    cache: dict[str, CacheEntry] = field(default_factory=dict)
    prior_month: dict[ProviderName, Decimal] = field(default_factory=dict)

    def add_company(self, ctx: CompanyContext, *, qualified: bool = False) -> None:
        self.companies[ctx.company_id] = ctx
        if qualified:
            self.qualified.append(ctx.company_id)

    def qualified_company_ids(self, limit: int) -> list[UUID]:
        return self.qualified[:limit]

    def load_company(self, company_id: UUID) -> CompanyContext:
        try:
            return self.companies[company_id]
        except KeyError:
            raise NotFoundError("company not found", details={"id": str(company_id)}) from None

    def create_run(self, run: EnrichmentRunRecord) -> None:
        self.runs[run.id] = run.model_copy(deep=True)

    def finish_run(self, run: EnrichmentRunRecord) -> None:
        self.runs[run.id] = run.model_copy(deep=True)

    def insert_contact(self, contact: EnrichmentContactRecord) -> None:
        if any(c.id == contact.id for c in self.contact_log):
            raise ValueError("enrichment contacts are append-only")
        self.contact_log.append(contact.model_copy(deep=True))

    def insert_attempt(self, attempt: ProviderAttemptRecord) -> None:
        if any(a.id == attempt.id for a in self.attempt_log):
            raise ValueError("provider attempts are append-only")
        self.attempt_log.append(attempt.model_copy(deep=True))

    def cache_get(self, key: str) -> CacheEntry | None:
        return self.cache.get(key)

    def cache_put(self, entry: CacheEntry) -> None:
        self.cache[entry.cache_key] = entry.model_copy(deep=True)

    def cache_hit(self, key: str) -> None:
        return None

    def month_credits(self, since: datetime) -> dict[ProviderName, Decimal]:
        out = dict(self.prior_month)
        for a in self.attempt_log:
            if a.provider in (ProviderName.HUNTER, ProviderName.APOLLO):
                out[a.provider] = out.get(a.provider, Decimal(0)) + a.credits
        return out

    def contacts(self, company_id: UUID) -> list[EnrichmentContactRecord]:
        return [c for c in reversed(self.contact_log) if c.company_id == company_id]
