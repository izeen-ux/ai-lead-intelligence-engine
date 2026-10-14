"""Request/response DTOs exchanged with provider adapters.

These describe *what* the business layer asks of a capability, independent of vendor. Adapters
map vendor payloads into these shapes; vendor-specific fields go into ``raw``/``facts`` dicts.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, HttpUrl, field_validator, model_validator

from app.domain.enums import (
    EmailVerificationStatus,
    EnrichmentTarget,
    LayaDecisionKind,
    PageType,
    ProviderName,
    QualificationDecision,
    QualificationDimension,
    SignalType,
    SourceType,
)
from app.domain.models import DomainModel, EnrichmentResult, LayaOutput, normalize_domain, utcnow


class ProviderStatus(DomainModel):
    """Config-only view of a provider. Building it never performs I/O."""

    name: ProviderName
    mode: str  # "mock" | "live"
    configured: bool  # credentials/endpoints present (always True for mocks)
    operational: bool  # can actually serve calls right now
    paid: bool  # calls consume paid credits
    detail: str | None = None


# --------------------------------------------------------------------------- discovery (Apify)
class DiscoveryQuery(DomainModel):
    """A discovery request. ``limit`` caps candidates returned; ``max_items`` and
    ``max_charge_usd`` are per-request spend controls that are clamped to the configured
    ceilings (they can lower, never raise, the limits)."""

    source: str = Field(min_length=1, max_length=64)  # logical source, e.g. "funding_news"
    keywords: list[str] = Field(default_factory=list, max_length=10)
    country_codes: list[str] = Field(default_factory=list, max_length=5)
    limit: int = Field(default=50, ge=1, le=1000)
    max_items: int | None = Field(default=None, ge=1)
    max_charge_usd: Decimal | None = Field(default=None, gt=0)
    actor_id: str | None = None  # override of APIFY_DISCOVERY_ACTOR_ID
    params: dict[str, Any] = Field(default_factory=dict)  # Actor-spec specific options

    @field_validator("keywords")
    @classmethod
    def _clean_keywords(cls, v: list[str]) -> list[str]:
        cleaned = [k.strip() for k in v if k.strip()]
        for k in cleaned:
            if len(k) > 300:
                raise ValueError("keyword too long (max 300 characters)")
        return cleaned

    @field_validator("country_codes")
    @classmethod
    def _country_codes(cls, v: list[str]) -> list[str]:
        codes = [c.strip().upper() for c in v]
        for c in codes:
            if len(c) != 2 or not c.isalpha():
                raise ValueError(f"invalid ISO 3166-1 alpha-2 country code: {c!r}")
        return codes


class CandidateRecord(DomainModel):
    company_name: str = Field(min_length=1, max_length=300)
    domain: str | None = None
    founder_name: str | None = None
    founder_role: str | None = None
    profile_url: HttpUrl | None = None
    source_url: HttpUrl
    source_type: SourceType
    source_title: str | None = None
    source_platform: str | None = None  # e.g. publisher host "inc42.com"
    published_at: AwareDatetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict)  # provenance / vendor extras (JSON-safe)

    @field_validator("domain")
    @classmethod
    def _normalize(cls, v: str | None) -> str | None:
        return normalize_domain(v) if v else None


class DiscoveryResult(DomainModel):
    provider: ProviderName
    actor_id: str | None = None
    run_ref: str | None = None  # e.g. Apify run id, for audit
    provider_status: str | None = None
    provider_input: dict[str, Any] = Field(default_factory=dict)  # exactly what was sent
    max_items: int | None = None
    max_charge_usd: Decimal | None = None
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    raw_item_count: int = Field(default=0, ge=0)
    skipped_count: int = Field(default=0, ge=0)  # raw results that were not candidates
    candidates: list[CandidateRecord] = Field(default_factory=list)
    estimated_cost_usd: Decimal | None = Field(default=None, ge=0)  # pre-flight worst case
    cost_usd: Decimal | None = Field(default=None, ge=0)  # reported by the provider
    charged_events: dict[str, int] = Field(default_factory=dict)


# ------------------------------------------------------------- web search (Firecrawl; Brave later)
class WebSearchQuery(DomainModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=5, ge=1, le=10)  # <=10 keeps Firecrawl search at 2 credits
    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")


class WebSearchHit(DomainModel):
    url: str
    title: str | None = None
    description: str | None = None
    position: int = Field(ge=1)


class WebSearchResult(DomainModel):
    provider: ProviderName
    query: str
    hits: list[WebSearchHit] = Field(default_factory=list)
    credits_used: int = Field(default=0, ge=0)


# --------------------------------------------------------------- website (Firecrawl / Crawl4AI)
class WebsiteExtractionRequest(DomainModel):
    """Crawl one company's official site within a page budget (and a credit cap for paid
    providers). Page selection policy lives in ``app.domain.website``."""

    domain: str
    page_budget: int = Field(default=8, ge=1, le=20)
    max_credits: int | None = Field(default=None, ge=1)  # paid providers only

    @field_validator("domain")
    @classmethod
    def _normalize(cls, v: str) -> str:
        return normalize_domain(v)


class WebPage(DomainModel):
    url: HttpUrl
    page_type: PageType = PageType.OTHER
    markdown: str
    title: str | None = None
    description: str | None = None
    status_code: int | None = None
    links: list[str] = Field(default_factory=list)  # outgoing links (social links -> metadata)
    fetched_at: AwareDatetime = Field(default_factory=utcnow)


class WebsiteExtractionResult(DomainModel):
    provider: ProviderName  # records which crawler produced the data
    domain: str
    pages: list[WebPage] = Field(default_factory=list)
    facts: dict[str, Any] = Field(default_factory=dict)
    social_links: dict[str, str] = Field(default_factory=dict)  # metadata only, never fetched
    sufficient: bool  # False -> caller may route to the Crawl4AI fallback
    reason: str | None = None  # why insufficient
    pages_attempted: int = Field(default=0, ge=0)
    failed_urls: list[str] = Field(default_factory=list)
    credits_used: int = Field(default=0, ge=0)


# ------------------------------------------------------------------ signal research (Agent-Reach)
class SignalResearchRequest(DomainModel):
    """Research one company. Budgets are hard limits the provider must respect."""

    company_name: str = Field(min_length=1)
    normalized_name: str | None = None  # for relevance matching (defaults to lowercased name)
    domain: str | None = None
    founder_name: str | None = None
    questions: list[str] = Field(default_factory=list)  # topical hints, e.g. "hiring"
    context_terms: list[str] = Field(default_factory=list)  # disambiguation words
    country: str | None = Field(default="IN", pattern=r"^[A-Z]{2}$")
    max_queries: int = Field(default=5, ge=1, le=10)
    max_results: int = Field(default=20, ge=1, le=200)  # processed items, across all queries


class EvidenceItem(DomainModel):
    signal_type: SignalType
    url: HttpUrl
    snippet: str  # verbatim text the signal is based on (never invented)
    title: str | None = None
    platform: str | None = None  # e.g. "news:Entrackr", "youtube:Founder Thesis"
    channel: str | None = None  # research channel, e.g. "news_rss"
    query: str | None = None  # query that surfaced it (provenance)
    published_at: AwareDatetime | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_notes: list[str] = Field(default_factory=list)


class ResearchQueryLog(DomainModel):
    channel: str
    query: str
    status: str  # ok | empty | error | policy_blocked | unsupported
    items_returned: int = Field(default=0, ge=0)
    items_processed: int = Field(default=0, ge=0)
    error_code: str | None = None
    error_message: str | None = None


class SignalResearchResult(DomainModel):
    provider: ProviderName
    items: list[EvidenceItem] = Field(default_factory=list)
    queries: list[ResearchQueryLog] = Field(default_factory=list)
    items_processed: int = Field(default=0, ge=0)  # findings examined (<= max_results)
    items_dropped: int = Field(default=0, ge=0)  # not about the company / no signal cue
    channels_used: list[str] = Field(default_factory=list)
    channels_disabled: dict[str, str] = Field(default_factory=dict)  # name -> reason


# ------------------------------------------------------------------------- qualification (Laya)
class LayaQuestion(DomainModel):
    """One typed Laya question (upstream shape: ``{type, instructions, criteria}``).

    * CHOICE      -> ``criteria``: label -> description (answer = label)
    * SCORE       -> ``criteria``: level descriptions, index 0 first (answer = expected level)
    * PROBABILITY -> Laya ``noul``; ``criteria`` keyed ``false``/``true`` (answer = P(true))
    """

    dimension: QualificationDimension
    kind: LayaDecisionKind
    instructions: str = Field(min_length=1, max_length=1000)
    criteria: dict[str, str] | list[str]

    @model_validator(mode="after")
    def _criteria_shape(self) -> Self:
        c = self.criteria
        if self.kind is LayaDecisionKind.CHOICE and not (isinstance(c, dict) and len(c) >= 2):
            raise ValueError("CHOICE needs >= 2 labelled options")
        if self.kind is LayaDecisionKind.SCORE and not (isinstance(c, list) and len(c) >= 2):
            raise ValueError("SCORE needs >= 2 ordered levels")
        if self.kind is LayaDecisionKind.PROBABILITY and not (
            isinstance(c, dict) and set(c) == {"false", "true"}
        ):
            raise ValueError("PROBABILITY (noul) criteria must be keyed exactly 'false'/'true'")
        return self

    def to_laya(self) -> dict[str, Any]:
        kind = "noul" if self.kind is LayaDecisionKind.PROBABILITY else self.kind.value
        return {"type": kind, "instructions": self.instructions, "criteria": self.criteria}


class QualificationRequest(DomainModel):
    subject_id: UUID  # company id (no PII)
    state: dict[str, Any]  # compact evidence bundle (see app.domain.qualification.bundle)
    questions: list[LayaQuestion] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def _unique_dimensions(self) -> Self:
        dims = [q.dimension for q in self.questions]
        if len(dims) != len(set(dims)):
            raise ValueError("one question per dimension")
        return self


class QualificationResult(DomainModel):
    provider: ProviderName
    outputs: list[LayaOutput]
    model_version: str | None = None  # checkpoint that answered (from the sidecar)
    latency_ms: int | None = Field(default=None, ge=0)

    def output(self, dimension: QualificationDimension) -> LayaOutput | None:
        return next((o for o in self.outputs if o.dimension is dimension), None)


# ------------------------------------------------------------------ reasoning (Techno-AI-Router)
class ChatMessage(DomainModel):
    role: str = Field(pattern=r"^(system|user|assistant)$")
    content: str = Field(min_length=1)


class ChatRequest(DomainModel):
    """One non-streaming chat completion through the router. ``model`` None = model policy."""

    messages: list[ChatMessage] = Field(min_length=1)
    model: str | None = None
    purpose: str = Field(default="chat", max_length=40)  # for logs/metrics only
    # Optional DOMAIN JSON Schema of the expected answer. Providers that support structured
    # output adapt it to their own request format; the domain validation stays authoritative.
    response_schema: dict[str, Any] | None = None
    response_schema_name: str = Field(default="response", pattern=r"^[A-Za-z0-9_-]{1,64}$")


class ChatUsage(DomainModel):
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class ChatResult(DomainModel):
    provider: ProviderName
    content: str
    finish_reason: str | None = None
    model: str  # model that answered (router response)
    requested_model: str
    gateway: str | None = None  # x_router.gateway
    upstream_provider: str | None = None  # x_router.provider
    usage: ChatUsage = Field(default_factory=ChatUsage)
    latency_ms: int = Field(default=0, ge=0)
    attempts: int = Field(default=1, ge=1)
    cost_usd: Decimal | None = Field(default=None, ge=0)  # from the live catalog price


class RouterModel(DomainModel):
    """One entry of the router's live catalog (GET /v1/models)."""

    provider: str
    modality: str
    model: str
    price: str | None = None
    price_in_per_1m_usd: Decimal | None = Field(default=None, ge=0)
    price_out_per_1m_usd: Decimal | None = Field(default=None, ge=0)
    enabled: bool = False
    tags: list[str] = Field(default_factory=list)

    @property
    def is_free(self) -> bool:
        return self.price_in_per_1m_usd == 0 and self.price_out_per_1m_usd == 0


# ------------------------------------------------------------------ LLM review (REVIEW cases only)
class LlmReviewRequest(DomainModel):
    subject_id: UUID
    bundle: dict[str, Any]  # the same evidence bundle Laya saw (signal/cluster ids inside)
    allowed_evidence_ids: list[str] = Field(default_factory=list)
    hard_rule_summary: list[dict[str, Any]] = Field(default_factory=list)
    qualified_allowed: bool  # False -> the reviewer may not return QUALIFIED
    # "decision": second opinion on a REVIEW case. "attribution": are the listed funding events
    # about THIS company (same business as its verified website) or a namesake?
    task: Literal["decision", "attribution"] = "decision"
    company_profile: dict[str, str] = Field(default_factory=dict)  # verified website facts
    question_event_ids: list[str] = Field(default_factory=list)  # events the task is about


class LlmReviewResult(DomainModel):
    provider: ProviderName
    decision: QualificationDecision
    confidence: float = Field(ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list, max_length=10)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    model: str | None = None


# ---------------------------------------------------- enrichment (enrichment-kit, Hunter, Apollo)
class EnrichmentRequest(DomainModel):
    """One person (or company) to enrich. In Phase 6 ``lead_id`` is the QUALIFIED decision id
    the request is authorised by. ``providers`` is the budget-filtered waterfall order."""

    lead_id: UUID
    target: EnrichmentTarget
    person_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    company_name: str | None = None
    domain: str | None = None
    linkedin_url: HttpUrl | None = None
    email_candidate: str | None = None
    min_confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    early_exit_confidence: float = Field(default=0.85, ge=0.0, le=1.0)
    providers: list[ProviderName] = Field(
        default_factory=lambda: [ProviderName.HUNTER, ProviderName.APOLLO]
    )
    # Optional phone capability (budgeted by the caller). False = providers are told not to
    # return a number and any number in a response is dropped.
    include_phone: bool = False

    @field_validator("domain")
    @classmethod
    def _normalize(cls, v: str | None) -> str | None:
        return normalize_domain(v) if v else None


class EnrichmentOutcome(DomainModel):
    """Waterfall result: the accepted record plus every attempt (for cost/audit)."""

    orchestrator: ProviderName
    final: EnrichmentResult | None
    attempts: list[EnrichmentResult] = Field(default_factory=list)
    total_cost_usd: Decimal | None = Field(default=None, ge=0)  # upstream per-call estimate


class EmailVerificationResult(DomainModel):
    provider: ProviderName
    email: str
    status: EmailVerificationStatus
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    error_code: str | None = None  # typed failure kind (timeout, rate_limited, ...)
    http_status: int | None = None
    checked_at: AwareDatetime = Field(default_factory=utcnow)


class PeopleSearchRequest(DomainModel):
    """Decision-maker discovery on a VERIFIED domain (Hunter Domain Search via the sidecar)."""

    domain: str
    limit: int = Field(default=5, ge=1, le=10)

    @field_validator("domain")
    @classmethod
    def _normalize(cls, v: str) -> str:
        return normalize_domain(v)


class DiscoveredPerson(DomainModel):
    first_name: str | None = None
    last_name: str | None = None
    position: str | None = None
    seniority: str | None = None
    email: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    verification_status: str | None = None
    verification_date: str | None = None
    sources_count: int = Field(default=0, ge=0)


class PeopleSearchResult(DomainModel):
    provider: ProviderName
    people: list[DiscoveredPerson] = Field(default_factory=list)
    error_code: str | None = None
    credits_used: int = Field(default=0, ge=0)
