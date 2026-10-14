"""SQLAlchemy ORM tables (persistence only - never exposed to business logic).

Phase 2 adds the minimum durable discovery entities:

* ``discovery_runs``  - one row per discovery request / provider job (audit + cost).
* ``companies``       - the deduplicated candidate pool (unique ``dedupe_key``).
* ``sources``         - deduplicated evidence URLs (unique ``url_hash``).
* ``discovery_hits``  - provenance: which run found which company at which source.

Phase 3 adds website intelligence:

* ``companies`` gains domain-resolution state; ``domain`` is unique once verified.
* ``domain_resolutions`` - every resolution attempt with scored candidates (audit).
* ``website_crawls``      - every crawl attempt (primary or fallback), budget, facts.
* ``website_pages``       - extracted page content per company (unique per URL).
* ``company_merges``      - audit of name-keyed -> domain-keyed merges; also an alias so later
                            discovery of the old name maps to the surviving company.

Repositories translate rows to/from domain models (``app/repositories/``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

TZ = DateTime(timezone=True)
_EMPTY_JSON = text("'{}'::jsonb")


class DiscoveryRunRow(Base):
    __tablename__ = "discovery_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[str | None] = mapped_column(String(200))
    provider_run_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    provider_status: Mapped[str | None] = mapped_column(String(32))
    query: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=_EMPTY_JSON)
    provider_input: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_JSON
    )
    max_items: Mapped[int | None] = mapped_column(Integer)
    max_charge_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    started_at: Mapped[datetime | None] = mapped_column(TZ)
    finished_at: Mapped[datetime | None] = mapped_column(TZ)
    raw_item_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    candidate_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    new_company_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    skipped_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    charged_events: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_JSON
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','succeeded','failed','cancelled')", name="status_valid"
        ),
    )


class CompanyRow(Base):
    __tablename__ = "companies"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str] = mapped_column(String(300))
    normalized_name: Mapped[str] = mapped_column(String(300), index=True)
    # Canonical official domain - set ONLY once verified against the homepage (Phase 3).
    domain: Mapped[str | None] = mapped_column(String(253))
    domain_status: Mapped[str] = mapped_column(
        String(16), default="unresolved", server_default="unresolved", index=True
    )
    domain_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    domain_source: Mapped[str | None] = mapped_column(String(32))  # e.g. firecrawl_search
    domain_verified_at: Mapped[datetime | None] = mapped_column(TZ)
    website_crawled_at: Mapped[datetime | None] = mapped_column(TZ)
    signals_researched_at: Mapped[datetime | None] = mapped_column(TZ)  # Phase 4
    description: Mapped[str | None] = mapped_column(Text)
    industry: Mapped[str | None] = mapped_column(String(200))
    location: Mapped[str | None] = mapped_column(String(200))
    country_code: Mapped[str | None] = mapped_column(String(2))
    employee_count: Mapped[int | None] = mapped_column(Integer)
    times_seen: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    first_seen_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())

    __table_args__ = (
        # One company per verified domain: the hard identity used for merges.
        Index(
            "uq_companies_domain",
            "domain",
            unique=True,
            postgresql_where=text("domain IS NOT NULL"),
        ),
        CheckConstraint(
            "domain_status IN ('unresolved','resolved','verified','review','not_found','failed')",
            name="domain_status_valid",
        ),
    )


class SourceRow(Base):
    __tablename__ = "sources"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    url_hash: Mapped[str] = mapped_column(String(64), unique=True)
    source_type: Mapped[str] = mapped_column(String(32))
    platform: Mapped[str | None] = mapped_column(String(200))
    title: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(TZ)
    retrieved_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    content_hash: Mapped[str | None] = mapped_column(String(64))
    provider: Mapped[str | None] = mapped_column(String(32))
    provider_ref: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class DiscoveryHitRow(Base):
    __tablename__ = "discovery_hits"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("discovery_runs.id", ondelete="CASCADE"), index=True
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), index=True
    )
    was_new_company: Mapped[bool] = mapped_column(default=False, server_default=false())
    candidate: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_JSON
    )  # normalized record
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "run_id", "company_id", "source_id", name="uq_discovery_hits_run_company_source"
        ),
    )


class DomainResolutionRow(Base):
    __tablename__ = "domain_resolutions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(32))
    query: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))
    chosen_domain: Mapped[str | None] = mapped_column(String(253))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    reason: Mapped[str | None] = mapped_column(Text)
    candidates: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    credits_used: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class WebsiteCrawlRow(Base):
    __tablename__ = "website_crawls"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    domain: Mapped[str] = mapped_column(String(253))
    provider: Mapped[str] = mapped_column(String(32))
    is_fallback: Mapped[bool] = mapped_column(default=False, server_default=false())
    status: Mapped[str] = mapped_column(String(16))
    sufficient: Mapped[bool] = mapped_column(default=False, server_default=false())
    reason: Mapped[str | None] = mapped_column(Text)
    identity_verified: Mapped[bool | None] = mapped_column()
    page_budget: Mapped[int] = mapped_column(Integer)
    pages_attempted: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    pages_succeeded: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    credits_used: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    facts: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=_EMPTY_JSON)
    social_links: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_JSON
    )
    failed_urls: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(TZ)
    finished_at: Mapped[datetime | None] = mapped_column(TZ)

    __table_args__ = (
        CheckConstraint(
            "status IN ('succeeded','insufficient','failed','skipped')", name="status_valid"
        ),
    )


class WebsitePageRow(Base):
    __tablename__ = "website_pages"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    crawl_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("website_crawls.id", ondelete="SET NULL"), index=True
    )
    url: Mapped[str] = mapped_column(Text)
    url_hash: Mapped[str] = mapped_column(String(64))
    page_type: Mapped[str] = mapped_column(String(16))
    provider: Mapped[str] = mapped_column(String(32))
    status_code: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    markdown: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("company_id", "url_hash", name="uq_website_pages_company_url"),
    )


class CompanyMergeRow(Base):
    __tablename__ = "company_merges"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    from_company_id: Mapped[uuid.UUID] = mapped_column(Uuid)  # row no longer exists
    into_company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    from_dedupe_key: Mapped[str] = mapped_column(String(320), unique=True)  # alias
    from_name: Mapped[str] = mapped_column(String(300))
    domain: Mapped[str] = mapped_column(String(253))
    reason: Mapped[str] = mapped_column(String(64))
    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_JSON
    )
    merged_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class SignalResearchRunRow(Base):
    """One Agent-Reach research run for one company (Phase 4): budget, queries, outcome."""

    __tablename__ = "signal_research_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    channels_used: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    channels_disabled: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_JSON
    )
    queries: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    max_queries: Mapped[int] = mapped_column(Integer)
    max_items: Mapped[int] = mapped_column(Integer)
    queries_used: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    items_processed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    items_dropped: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    signals_created: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    signals_duplicate: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(TZ)
    finished_at: Mapped[datetime | None] = mapped_column(TZ)

    __table_args__ = (
        CheckConstraint("status IN ('succeeded','empty','failed','skipped')", name="status_valid"),
    )


class SignalRow(Base):
    """A normalized business signal with provenance (source row + channel/platform/query)."""

    __tablename__ = "signals"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("signal_research_runs.id", ondelete="SET NULL"), index=True
    )
    signal_type: Mapped[str] = mapped_column(String(32), index=True)
    summary: Mapped[str] = mapped_column(Text)
    evidence: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3))
    channel: Mapped[str | None] = mapped_column(String(32))
    platform: Mapped[str | None] = mapped_column(String(200))
    query: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(TZ)
    evidence_notes: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "company_id", "signal_type", "source_id", name="uq_signals_company_type_source"
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    )


class QualificationRunRow(Base):
    """One qualification batch (Phase 5): mode, policy version, ICP snapshot, counts."""

    __tablename__ = "qualification_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    policy_version: Mapped[str] = mapped_column(String(32))
    laya_mode: Mapped[str] = mapped_column(String(16))  # mock | live
    llm_review_mode: Mapped[str] = mapped_column(String(16))  # disabled | mock | live
    icp_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_JSON
    )
    companies_evaluated: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    qualified_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    review_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    not_qualified_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(TZ)
    finished_at: Mapped[datetime | None] = mapped_column(TZ)

    __table_args__ = (
        CheckConstraint(
            "status IN ('running','succeeded','failed','cancelled')", name="status_valid"
        ),
    )


class QualificationDecisionRow(Base):
    """Immutable qualification history: one row per company per run. Never updated - a database
    trigger (migration 0005) rejects UPDATE; re-qualification inserts a new row."""

    __tablename__ = "qualification_decisions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("qualification_runs.id", ondelete="CASCADE"), index=True
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    decision: Mapped[str] = mapped_column(String(16), index=True)
    path: Mapped[str] = mapped_column(String(16))
    hard_rules_passed: Mapped[bool] = mapped_column()
    hard_rule_results: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    reasons: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    laya_status: Mapped[str] = mapped_column(String(16))
    laya_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    laya_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    laya_model_version: Mapped[str | None] = mapped_column(String(100))
    llm_review_status: Mapped[str] = mapped_column(String(16))
    llm_review: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    policy_version: Mapped[str] = mapped_column(String(32))
    icp_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_JSON
    )
    evidence_bundle: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_JSON
    )
    evidence_signal_ids: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    signal_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    event_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    decided_at: Mapped[datetime] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())

    __table_args__ = (
        Index("ix_qualification_decisions_company_decided", "company_id", "decided_at"),
        CheckConstraint(
            "decision IN ('qualified','review','not_qualified')", name="decision_valid"
        ),
        CheckConstraint("path IN ('funding','intent','both','none')", name="path_valid"),
        CheckConstraint("laya_status IN ('ok','skipped','error')", name="laya_status_valid"),
        CheckConstraint(
            "llm_review_status IN ('ok','skipped','error')", name="llm_review_status_valid"
        ),
        CheckConstraint(
            "decision <> 'qualified' OR hard_rules_passed", name="qualified_requires_hard_rules"
        ),
        CheckConstraint(
            "(score IS NULL OR (score >= 0 AND score <= 1)) AND "
            "(confidence IS NULL OR (confidence >= 0 AND confidence <= 1)) AND "
            "(laya_confidence IS NULL OR (laya_confidence >= 0 AND laya_confidence <= 1))",
            name="bounded_scores",
        ),
    )


_VERIFICATION_VALUES = (
    "'unverified','valid','invalid','accept_all','unknown','webmail','disposable','pending'"
)


class EnrichmentRunRow(Base):
    """One enrichment batch (Phase 6): mode, policy version, config/budget snapshot, usage."""

    __tablename__ = "enrichment_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    mode: Mapped[str] = mapped_column(String(16))
    policy_version: Mapped[str] = mapped_column(String(32))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=_EMPTY_JSON)
    companies_evaluated: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    contacts_created: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    verified_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cached_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    blocked_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failed_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    hunter_credits: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0, server_default="0")
    apollo_credits: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0, server_default="0")
    sidecar_calls: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    estimated_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), default=0, server_default="0"
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(TZ)
    finished_at: Mapped[datetime | None] = mapped_column(TZ)

    __table_args__ = (
        CheckConstraint(
            "status IN ('running','succeeded','failed','cancelled')", name="status_valid"
        ),
        CheckConstraint("mode IN ('mock','live')", name="mode_valid"),
    )


class EnrichmentContactRow(Base):
    """Normalized enrichment outcome per person (append-only; trigger in migration 0006)."""

    __tablename__ = "enrichment_contacts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("enrichment_runs.id", ondelete="CASCADE"), index=True
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    qualification_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("qualification_decisions.id", ondelete="CASCADE"), index=True
    )
    person_key: Mapped[str | None] = mapped_column(String(400), index=True)
    full_name: Mapped[str | None] = mapped_column(String(200))
    first_name: Mapped[str | None] = mapped_column(String(100))
    last_name: Mapped[str | None] = mapped_column(String(100))
    title: Mapped[str | None] = mapped_column(String(300))
    role: Mapped[str | None] = mapped_column(String(64))
    role_rank: Mapped[int | None] = mapped_column(Integer)
    linkedin_url: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(String(40))
    phone_status: Mapped[str] = mapped_column(
        String(20), default="not_requested", server_default="not_requested"
    )
    email: Mapped[str | None] = mapped_column(String(320))
    email_verification: Mapped[str] = mapped_column(String(16))
    verification_provider: Mapped[str | None] = mapped_column(String(32))
    verified_at: Mapped[datetime | None] = mapped_column(TZ)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    status: Mapped[str] = mapped_column(String(20), index=True)
    provider: Mapped[str | None] = mapped_column(String(32))
    from_cache: Mapped[bool] = mapped_column(default=False, server_default=false())
    identity: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_JSON
    )
    provenance: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_JSON
    )
    reasons: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    credits_used: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_JSON
    )
    estimated_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), default=0, server_default="0"
    )
    policy_version: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "phone_status IN ('not_requested','found','not_found','budget_exhausted')",
            name="phone_status_valid",
        ),
        CheckConstraint(
            "(phone IS NOT NULL) = (phone_status = 'found')", name="phone_matches_status"
        ),
        CheckConstraint(
            "status IN ('verified','unverified','invalid_email','not_found','no_person',"
            "'ambiguous','budget_exhausted','blocked','failed')",
            name="status_valid",
        ),
        CheckConstraint(
            f"email_verification IN ({_VERIFICATION_VALUES})", name="verification_valid"
        ),
        CheckConstraint(
            "status <> 'verified' OR (email IS NOT NULL AND email_verification = 'valid')",
            name="verified_requires_valid_email",
        ),
        CheckConstraint(
            "status IN ('blocked','no_person','ambiguous') OR qualification_id IS NOT NULL",
            name="enriched_requires_qualification",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="confidence_range"
        ),
    )


class EnrichmentAttemptRow(Base):
    """Every provider operation (append-only; monthly budgets are summed from here)."""

    __tablename__ = "enrichment_attempts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("enrichment_runs.id", ondelete="CASCADE"), index=True
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    person_key: Mapped[str | None] = mapped_column(String(400))
    provider: Mapped[str] = mapped_column(String(32))
    operation: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(64))
    http_status: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    credits: Mapped[Decimal] = mapped_column(Numeric(8, 2), default=0, server_default="0")
    estimated_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), default=0, server_default="0"
    )
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=_EMPTY_JSON)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())

    __table_args__ = (
        Index("ix_enrichment_attempts_provider_created", "provider", "created_at"),
        CheckConstraint("status IN ('hit','miss','error','skipped')", name="status_valid"),
        CheckConstraint("credits >= 0", name="credits_non_negative"),
    )


class EnrichmentCacheRow(Base):
    """Cache of the latest enrichment per (verified domain, person). Mutable by design;
    history lives in enrichment_contacts / enrichment_attempts."""

    __tablename__ = "enrichment_cache"

    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    person_key: Mapped[str] = mapped_column(String(400))
    domain: Mapped[str] = mapped_column(String(253))
    email: Mapped[str | None] = mapped_column(String(320))
    email_verification: Mapped[str] = mapped_column(String(16))
    verified_at: Mapped[datetime | None] = mapped_column(TZ)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    provider: Mapped[str | None] = mapped_column(String(32))
    title: Mapped[str | None] = mapped_column(String(300))
    linkedin_url: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(String(40))
    phone_status: Mapped[str] = mapped_column(
        String(20), default="not_requested", server_default="not_requested"
    )
    provenance: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_JSON
    )
    cached_at: Mapped[datetime] = mapped_column(TZ)
    expires_at: Mapped[datetime] = mapped_column(TZ, index=True)
    hits: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    __table_args__ = (
        CheckConstraint(
            "phone_status IN ('not_requested','found','not_found','budget_exhausted')",
            name="phone_status_valid",
        ),
        CheckConstraint(
            "(phone IS NOT NULL) = (phone_status = 'found')", name="phone_matches_status"
        ),
        CheckConstraint(
            f"email_verification IN ({_VERIFICATION_VALUES})", name="verification_valid"
        ),
    )


class AnalysisRunRow(Base):
    """One lead-analysis batch (Phase 7): model policy, versions, usage, cost."""

    __tablename__ = "analysis_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    mode: Mapped[str] = mapped_column(String(16))
    policy_version: Mapped[str] = mapped_column(String(32))
    prompt_version: Mapped[str] = mapped_column(String(40))
    catalog_version: Mapped[str] = mapped_column(String(40))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=_EMPTY_JSON)
    analyses_created: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    succeeded_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    rejected_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failed_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    blocked_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=0, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(TZ)
    finished_at: Mapped[datetime | None] = mapped_column(TZ)

    __table_args__ = (
        CheckConstraint(
            "status IN ('running','succeeded','failed','cancelled')", name="status_valid"
        ),
        CheckConstraint("mode IN ('mock','live')", name="mode_valid"),
    )


class LeadAnalysisRow(Base):
    """Immutable AI lead analysis (append-only; trigger in migration 0007)."""

    __tablename__ = "lead_analyses"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), index=True
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    qualification_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("qualification_decisions.id", ondelete="CASCADE"), index=True
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("enrichment_contacts.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(20), index=True)
    analysis: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    recommended_offer_id: Mapped[str | None] = mapped_column(String(64), index=True)
    primary_trigger: Mapped[str | None] = mapped_column(String(32))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    supporting_evidence_ids: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    evidence_bundle: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_JSON
    )
    evidence_signal_ids: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    catalog_version: Mapped[str] = mapped_column(String(40))
    model: Mapped[str | None] = mapped_column(String(200))
    gateway: Mapped[str | None] = mapped_column(String(64))
    provider: Mapped[str | None] = mapped_column(String(64))
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=_EMPTY_JSON)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    policy_version: Mapped[str] = mapped_column(String(32))
    prompt_version: Mapped[str] = mapped_column(String(40))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    raw_excerpt: Mapped[str | None] = mapped_column(Text)
    analyzed_at: Mapped[datetime] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())

    __table_args__ = (
        Index("ix_lead_analyses_company_analyzed", "company_id", "analyzed_at"),
        CheckConstraint(
            "status IN ('succeeded','rejected_output','failed','blocked','budget_exhausted')",
            name="status_valid",
        ),
        CheckConstraint(
            "status <> 'succeeded' OR (analysis IS NOT NULL AND recommended_offer_id IS NOT NULL"
            " AND qualification_id IS NOT NULL AND contact_id IS NOT NULL)",
            name="succeeded_requires_grounding",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="confidence_range"
        ),
    )


class ExportRunRow(Base):
    """One output export (Phase 8): Google Sheets projection of PostgreSQL state. Updated only
    to record its own completion; per-row history lives in ``export_items``."""

    __tablename__ = "export_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    mode: Mapped[str] = mapped_column(String(16))  # dry_run | mock | live
    target: Mapped[str] = mapped_column(String(32))
    spreadsheet_ref: Mapped[str | None] = mapped_column(String(16))  # masked id, never the full id
    worksheets: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=_EMPTY_JSON)
    leads_considered: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    inserted_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    updated_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    unchanged_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    withdrawn_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    skipped_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failed_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    duplicate_rows_found: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    api_calls: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(TZ)
    finished_at: Mapped[datetime | None] = mapped_column(TZ)

    __table_args__ = (
        CheckConstraint(
            "status IN ('running','succeeded','failed','cancelled')", name="status_valid"
        ),
        CheckConstraint("mode IN ('dry_run','mock','live')", name="mode_valid"),
    )


class ExportItemRow(Base):
    """One planned/applied row change per export run (append-only; trigger in migration 0008)."""

    __tablename__ = "export_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("export_runs.id", ondelete="CASCADE"), index=True
    )
    worksheet: Mapped[str] = mapped_column(String(100))
    lead_id: Mapped[str] = mapped_column(String(40), index=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    qualification_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    analysis_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(16))
    row_number: Mapped[int | None] = mapped_column(Integer)
    row_hash: Mapped[str | None] = mapped_column(String(64))
    export_status: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "action IN ('insert','update','unchanged','withdraw','skip','fail')",
            name="action_valid",
        ),
    )


class PrescreenDecisionRow(Base):
    """Scrape / skip verdict from discovery data alone, taken BEFORE paid website
    intelligence (append-only; trigger in migration 0011)."""

    __tablename__ = "prescreen_decisions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    decision: Mapped[str] = mapped_column(String(16))
    reasons: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    laya_status: Mapped[str] = mapped_column(String(16))
    laya_fit: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    policy_version: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())

    __table_args__ = (
        CheckConstraint("decision IN ('scrape','skip')", name="decision_valid"),
        CheckConstraint("laya_status IN ('ok','skipped','error')", name="laya_status_valid"),
    )


class LeadOwnerRow(Base):
    """The team member whose pipeline run produced a company's lead (first owner is kept)."""

    __tablename__ = "lead_owners"

    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True
    )
    owner: Mapped[str] = mapped_column(String(120), index=True)
    pipeline_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("pipeline_runs.id", ondelete="SET NULL")
    )
    assigned_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class HumanReviewRow(Base):
    """Audit of every human REVIEW action (append-only; trigger in migration 0008). An applied
    action points to the NEW qualification decision it created - decisions are never edited."""

    __tablename__ = "human_reviews"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    action: Mapped[str] = mapped_column(String(16))
    outcome: Mapped[str] = mapped_column(String(16))
    reviewer: Mapped[str] = mapped_column(String(120))
    reason: Mapped[str] = mapped_column(Text)
    prior_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("qualification_decisions.id", ondelete="CASCADE"), index=True
    )
    resulting_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("qualification_decisions.id", ondelete="CASCADE"), index=True
    )
    hard_rules_passed: Mapped[bool | None] = mapped_column()
    hard_rule_results: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    refusal_code: Mapped[str | None] = mapped_column(String(64))
    policy_version: Mapped[str] = mapped_column(String(32))
    reviewed_at: Mapped[datetime] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())

    __table_args__ = (
        Index("ix_human_reviews_company_reviewed", "company_id", "reviewed_at"),
        CheckConstraint("action IN ('approve','reject','reopen')", name="action_valid"),
        CheckConstraint("outcome IN ('applied','refused')", name="outcome_valid"),
        CheckConstraint(
            "outcome <> 'applied' OR resulting_decision_id IS NOT NULL",
            name="applied_requires_decision",
        ),
        CheckConstraint(
            "outcome <> 'refused' OR refusal_code IS NOT NULL", name="refused_requires_code"
        ),
        CheckConstraint(
            "length(btrim(reviewer)) > 0 AND length(btrim(reason)) >= 3",
            name="reviewer_and_reason_required",
        ),
    )


class PipelineRunRow(Base):
    """One orchestrated pipeline run (Phase 9). Updated while it runs (stage results,
    heartbeat) and when it ends. ``active_slot`` is 1 only while RUNNING; its unique
    constraint guarantees a single running pipeline (duplicate triggers are refused)."""

    __tablename__ = "pipeline_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    active_slot: Mapped[int | None] = mapped_column(Integer)
    trigger: Mapped[str] = mapped_column(String(16))
    requested_by: Mapped[str | None] = mapped_column(String(120))
    mode: Mapped[str] = mapped_column(String(16))
    dry_run: Mapped[bool] = mapped_column()
    stages_requested: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    company_limit: Mapped[int] = mapped_column(Integer)
    stages: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=_EMPTY_JSON)
    estimated_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=0, server_default="0"
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(TZ)
    heartbeat_at: Mapped[datetime] = mapped_column(TZ)
    finished_at: Mapped[datetime | None] = mapped_column(TZ)

    __table_args__ = (
        UniqueConstraint("active_slot", name="uq_pipeline_runs_active_slot"),
        Index("ix_pipeline_runs_started", "started_at"),
        CheckConstraint(
            "status IN ('running','succeeded','failed','cancelled','abandoned')",
            name="status_valid",
        ),
        CheckConstraint("trigger IN ('cli','api','schedule')", name="trigger_valid"),
        CheckConstraint("mode IN ('mock','live')", name="mode_valid"),
        CheckConstraint(
            "(status = 'running' AND active_slot = 1) OR "
            "(status <> 'running' AND active_slot IS NULL)",
            name="slot_matches_status",
        ),
    )
