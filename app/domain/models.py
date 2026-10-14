"""Core domain contracts.

These are provider-agnostic Pydantic models. Provider adapters translate external payloads into
these types; business logic only ever sees these types. Persistence (SQLAlchemy ORM) is mapped
separately so the domain stays free of database concerns.

Design rules:
* ``extra="forbid"`` - unknown fields are bugs, not silently dropped data.
* All timestamps are timezone-aware UTC.
* Evidence first: signals must cite at least one ``Source``.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Self
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

from app.core.errors import InvalidStateTransitionError
from app.domain.enums import (
    CrawlStatus,
    DomainResolutionStatus,
    EmailVerificationStatus,
    EnrichmentStatus,
    EnrichmentTarget,
    GateDecision,
    LayaDecisionKind,
    LeadStatus,
    PipelineStage,
    ProviderName,
    QualificationDimension,
    ResearchRunStatus,
    ReviewStatus,
    RunStatus,
    SignalType,
    SourceType,
)
from app.domain.lifecycle import can_transition

_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SHA256_RE = r"^[0-9a-f]{64}$"


def utcnow() -> datetime:
    return datetime.now(UTC)


def normalize_domain(value: str) -> str:
    """Canonicalize a domain/URL to a bare host: ``https://www.Acme.io/x`` -> ``acme.io``."""
    raw = value.strip().lower()
    host = urlsplit(raw if "://" in raw else f"//{raw}").hostname or ""
    host = host.rstrip(".").removeprefix("www.")
    if not _DOMAIN_RE.match(host):
        raise ValueError(f"not a valid domain: {value!r}")
    return host


def compute_content_hash(content: str) -> str:
    """Stable hash used to detect duplicate/unchanged source content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, str_strip_whitespace=True)


class Entity(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    created_at: AwareDatetime = Field(default_factory=utcnow)
    updated_at: AwareDatetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- Company
class Company(Entity):
    name: str = Field(min_length=1, max_length=300)
    domain: str | None = None
    website: HttpUrl | None = None
    description: str | None = None
    industry: str | None = None
    location: str | None = None
    country_code: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")  # ISO 3166-1 alpha-2
    employee_count: int | None = Field(default=None, ge=0)
    source_ids: list[UUID] = Field(default_factory=list)
    domain_status: DomainResolutionStatus = DomainResolutionStatus.UNRESOLVED
    domain_confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("domain")
    @classmethod
    def _normalize_domain(cls, v: str | None) -> str | None:
        return normalize_domain(v) if v else None


# --------------------------------------------------------------------------- Founder
class Founder(Entity):
    name: str = Field(min_length=1, max_length=200)
    role: str | None = None
    company_id: UUID
    # keyed by platform, e.g. {"linkedin": "...", "x": "..."}
    profile_urls: dict[str, HttpUrl] = Field(default_factory=dict)
    enrichment_status: EnrichmentStatus | None = None  # None = enrichment never requested

    @field_validator("profile_urls")
    @classmethod
    def _lower_platform_keys(cls, v: dict[str, HttpUrl]) -> dict[str, HttpUrl]:
        return {k.strip().lower(): url for k, url in v.items()}


# --------------------------------------------------------------------------- Source
class Source(Entity):
    url: HttpUrl
    source_type: SourceType
    platform: str | None = None  # e.g. "linkedin", "tracxn", "company_site"
    title: str | None = None
    published_at: AwareDatetime | None = None
    retrieved_at: AwareDatetime = Field(default_factory=utcnow)
    content_hash: str | None = Field(default=None, pattern=_SHA256_RE)
    provider: ProviderName | None = None  # which adapter produced it
    provider_ref: str | None = None  # e.g. Apify run id, Firecrawl job id


# --------------------------------------------------------------------------- Signal
class FundingDetails(DomainModel):
    amount: Decimal | None = Field(default=None, ge=0)
    currency: str = Field(default="INR", pattern=r"^[A-Z]{3}$")
    round_name: str | None = None
    announced_on: date | None = None

    def within_cap(self, cap_inr: Decimal) -> bool | None:
        """``None`` when unknown (missing amount or non-INR; FX conversion is a later phase)."""
        if self.amount is None or self.currency != "INR":
            return None
        return self.amount <= cap_inr


class Signal(Entity):
    company_id: UUID
    founder_id: UUID | None = None
    signal_type: SignalType
    summary: str = Field(min_length=1)
    evidence: str | None = None  # quoted snippet supporting the summary
    source_ids: list[UUID] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    observed_at: AwareDatetime | None = None
    funding: FundingDetails | None = None

    @model_validator(mode="after")
    def _funding_only_on_funding_signals(self) -> Self:
        if self.funding is not None and self.signal_type is not SignalType.FUNDING:
            raise ValueError("funding details are only allowed on FUNDING signals")
        return self


# --------------------------------------------------------------------------- Qualification
class RuleResult(DomainModel):
    rule: str = Field(min_length=1)
    passed: bool
    detail: str | None = None


class LayaOutput(DomainModel):
    dimension: QualificationDimension
    kind: LayaDecisionKind
    value: str | float | bool
    # Laya ``answer_confidence`` (calibrated, comparable across question types).
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    probabilities: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _value_matches_kind(self) -> Self:
        v = self.value
        if self.kind is LayaDecisionKind.CHOICE and not isinstance(v, str):
            raise ValueError("CHOICE decisions must have a string value")
        if self.kind is LayaDecisionKind.SCORE and (
            isinstance(v, bool) or not isinstance(v, int | float)
        ):
            raise ValueError("SCORE decisions must have a numeric value")
        if self.kind is LayaDecisionKind.PROBABILITY and (
            isinstance(v, bool) or not isinstance(v, int | float) or not 0.0 <= v <= 1.0
        ):
            raise ValueError("PROBABILITY decisions must be a number in [0, 1]")
        return self


class Qualification(Entity):
    lead_id: UUID
    rule_results: list[RuleResult] = Field(default_factory=list)
    laya_outputs: list[LayaOutput] = Field(default_factory=list)
    decision: GateDecision | None = None
    review_status: ReviewStatus = ReviewStatus.NOT_REQUIRED
    reviewer: str | None = None  # e.g. "llm:claude-opus", "human:<name>"
    reasoning: str | None = None
    evidence_source_ids: list[UUID] = Field(default_factory=list)
    decided_at: AwareDatetime | None = None

    @property
    def hard_rules_passed(self) -> bool:
        return all(r.passed for r in self.rule_results)

    @model_validator(mode="after")
    def _gate_invariants(self) -> Self:
        if self.decision is GateDecision.ENRICH and not self.hard_rules_passed:
            raise ValueError("cannot ENRICH a lead that failed a hard rule")
        if self.decision is GateDecision.REVIEW and self.review_status is ReviewStatus.NOT_REQUIRED:
            raise ValueError("REVIEW decision requires a review_status other than NOT_REQUIRED")
        return self


# --------------------------------------------------------------------------- Lead
class StatusChange(DomainModel):
    from_status: LeadStatus
    to_status: LeadStatus
    at: AwareDatetime = Field(default_factory=utcnow)
    reason: str | None = None


class Lead(Entity):
    company_id: UUID
    founder_id: UUID | None = None
    status: LeadStatus = LeadStatus.DISCOVERED
    signal_ids: list[UUID] = Field(default_factory=list)
    qualification_id: UUID | None = None
    enrichment_result_ids: list[UUID] = Field(default_factory=list)
    # Filled by the AI analysis phase (Phase 7).
    pain_point: str | None = None
    ai_use_case: str | None = None
    offer_angle: str | None = None
    status_history: list[StatusChange] = Field(default_factory=list)

    def transition_to(self, target: LeadStatus, *, reason: str | None = None) -> None:
        """Move to ``target`` or raise ``InvalidStateTransitionError``. Records an audit entry."""
        if not can_transition(self.status, target):
            raise InvalidStateTransitionError(
                f"illegal lead transition {self.status} -> {target}",
                details={"lead_id": str(self.id), "from": self.status, "to": target},
            )
        if target is LeadStatus.ENRICHMENT_PENDING and self.qualification_id is None:
            raise InvalidStateTransitionError(
                "lead cannot enter enrichment without a qualification record",
                details={"lead_id": str(self.id)},
            )
        self.status_history.append(
            StatusChange(from_status=self.status, to_status=target, reason=reason)
        )
        self.status = target
        self.updated_at = utcnow()


# --------------------------------------------------------------------------- Enrichment
class EnrichmentResult(Entity):
    lead_id: UUID | None = None
    founder_id: UUID | None = None
    company_id: UUID | None = None
    target: EnrichmentTarget
    provider: ProviderName  # the vendor that returned the data (e.g. hunter)
    orchestrated_by: ProviderName | None = None  # e.g. enrichment_kit
    status: EnrichmentStatus
    fields: dict[str, Any] = Field(default_factory=dict)  # normalized returned attributes
    email: str | None = None
    email_verification: EmailVerificationStatus = EmailVerificationStatus.UNVERIFIED
    phone: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    cost_credits: Decimal | None = Field(default=None, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)
    attempted_at: AwareDatetime = Field(default_factory=utcnow)
    completed_at: AwareDatetime | None = None
    error: str | None = None
    error_code: str | None = None  # typed failure kind from the provider/sidecar

    @field_validator("email")
    @classmethod
    def _email_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.lower()
        if not _EMAIL_RE.match(v):
            raise ValueError("invalid email address")
        return v

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        if self.status is EnrichmentStatus.FAILED and not self.error:
            raise ValueError("FAILED enrichment must carry an error message")
        if self.email is None and self.email_verification is not EmailVerificationStatus.UNVERIFIED:
            raise ValueError("email_verification requires an email")
        if self.completed_at is not None and self.completed_at < self.attempted_at:
            raise ValueError("completed_at cannot precede attempted_at")
        return self


# --------------------------------------------------------------------------- PipelineRun
class RunError(DomainModel):
    stage: PipelineStage | None = None
    code: str
    message: str
    at: AwareDatetime = Field(default_factory=utcnow)


class PipelineRun(Entity):
    stage: PipelineStage | None = None  # None = full end-to-end run
    status: RunStatus = RunStatus.PENDING
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    counts: dict[str, int] = Field(default_factory=dict)  # e.g. {"discovered": 400}
    provider_calls: dict[ProviderName, int] = Field(default_factory=dict)
    provider_costs: dict[ProviderName, Decimal] = Field(default_factory=dict)
    errors: list[RunError] = Field(default_factory=list)

    def start(self) -> None:
        if self.status is not RunStatus.PENDING:
            raise InvalidStateTransitionError(f"run already {self.status}")
        self.status = RunStatus.RUNNING
        self.started_at = utcnow()

    def finish(self, status: RunStatus) -> None:
        if self.status is not RunStatus.RUNNING:
            raise InvalidStateTransitionError(f"cannot finish a run that is {self.status}")
        if status in (RunStatus.PENDING, RunStatus.RUNNING):
            raise InvalidStateTransitionError(f"{status} is not a terminal status")
        self.status = status
        self.finished_at = utcnow()

    @model_validator(mode="after")
    def _time_order(self) -> Self:
        if self.started_at and self.finished_at and self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")
        return self


# --------------------------------------------------------------------------- DiscoveryRun
class DiscoveryRun(Entity):
    """Audit record of one discovery request and the provider job that served it (Phase 2).

    Captures what was asked (``query``), what was sent to the provider (``provider_input``),
    the spend controls applied, what came back, what it cost and how it was deduplicated.
    """

    provider: ProviderName
    actor_id: str | None = None
    provider_run_id: str | None = None  # e.g. Apify run id
    status: RunStatus = RunStatus.PENDING
    query: dict[str, Any] = Field(default_factory=dict)
    provider_input: dict[str, Any] = Field(default_factory=dict)
    max_items: int | None = Field(default=None, ge=1)
    max_charge_usd: Decimal | None = Field(default=None, gt=0)
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    provider_status: str | None = None  # raw provider status, e.g. "SUCCEEDED"
    raw_item_count: int = Field(default=0, ge=0)
    candidate_count: int = Field(default=0, ge=0)  # normalized candidates received
    new_company_count: int = Field(default=0, ge=0)
    duplicate_count: int = Field(default=0, ge=0)  # already known or repeated in batch
    skipped_count: int = Field(default=0, ge=0)  # raw results that were not candidates
    estimated_cost_usd: Decimal | None = Field(default=None, ge=0)  # pre-flight worst case
    cost_usd: Decimal | None = Field(default=None, ge=0)  # reported by the provider
    charged_events: dict[str, int] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        if self.started_at and self.finished_at and self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")
        if self.status is RunStatus.FAILED and not self.error_code:
            raise ValueError("FAILED discovery run must carry an error_code")
        return self


# --------------------------------------------------------------------------- Website (Phase 3)
class DomainResolutionRecord(Entity):
    """One attempt to find a company's official website (audit trail incl. scored candidates)."""

    company_id: UUID
    provider: str  # provider name, or "operator" for a manual override
    query: str
    status: DomainResolutionStatus
    chosen_domain: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    reason: str | None = None
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    credits_used: int = Field(default=0, ge=0)
    error_code: str | None = None


class WebsiteCrawlRecord(Entity):
    """One crawl attempt (primary or fallback) of a company's official site."""

    company_id: UUID
    domain: str
    provider: ProviderName
    is_fallback: bool = False
    status: CrawlStatus
    sufficient: bool = False
    reason: str | None = None
    identity_verified: bool | None = None
    page_budget: int = Field(ge=1)
    pages_attempted: int = Field(default=0, ge=0)
    pages_succeeded: int = Field(default=0, ge=0)
    credits_used: int = Field(default=0, ge=0)
    facts: dict[str, Any] = Field(default_factory=dict)
    social_links: dict[str, str] = Field(default_factory=dict)
    failed_urls: list[str] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    started_at: AwareDatetime = Field(default_factory=utcnow)
    finished_at: AwareDatetime | None = None


# --------------------------------------------------------------------------- Research (Phase 4)
class SignalResearchRunRecord(Entity):
    """One signal-research run for one company: budgets, channels, queries, outcome."""

    company_id: UUID
    provider: ProviderName
    status: ResearchRunStatus
    channels_used: list[str] = Field(default_factory=list)
    channels_disabled: dict[str, str] = Field(default_factory=dict)
    queries: list[dict[str, Any]] = Field(default_factory=list)
    max_queries: int = Field(ge=1)
    max_items: int = Field(ge=1)
    queries_used: int = Field(default=0, ge=0)
    items_processed: int = Field(default=0, ge=0)
    items_dropped: int = Field(default=0, ge=0)
    signals_created: int = Field(default=0, ge=0)
    signals_duplicate: int = Field(default=0, ge=0)
    error_code: str | None = None
    error_message: str | None = None
    started_at: AwareDatetime = Field(default_factory=utcnow)
    finished_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _budget_respected(self) -> Self:
        if self.queries_used > self.max_queries:
            raise ValueError("research run exceeded its query budget")
        if self.items_processed > self.max_items:
            raise ValueError("research run exceeded its item budget")
        if self.status is ResearchRunStatus.FAILED and not self.error_code:
            raise ValueError("FAILED research run must carry an error_code")
        return self
