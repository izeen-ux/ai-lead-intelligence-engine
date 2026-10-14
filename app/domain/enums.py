"""Domain enums. Stored as their string values so they are stable in the DB and in exports."""

from __future__ import annotations

from enum import StrEnum


class ProviderName(StrEnum):
    APIFY = "apify"
    FIRECRAWL = "firecrawl"
    AGENT_REACH = "agent_reach"
    CRAWL4AI = "crawl4ai"
    LAYA = "laya"
    ENRICHMENT_KIT = "enrichment_kit"
    HUNTER = "hunter"
    APOLLO = "apollo"
    AI_ROUTER = "ai_router"  # Techno-AI-Router: DEPRECATED in Phase 10 (legacy, opt-in only)
    OPENAI = "openai"  # active reasoning provider (Phase 10; Responses API)
    ANTHROPIC = "anthropic"  # Claude (REASONING_PROVIDER=anthropic, owner decision 2026-10-14)


class LeadStatus(StrEnum):
    """Lead lifecycle. Allowed transitions live in ``app.domain.lifecycle``."""

    DISCOVERED = "discovered"
    RESEARCHED = "researched"
    QUALIFIED = "qualified"
    REVIEW = "review"
    REJECTED = "rejected"
    ENRICHMENT_PENDING = "enrichment_pending"
    ENRICHED = "enriched"
    READY = "ready"


class SourceType(StrEnum):
    COMPANY_WEBSITE = "company_website"
    SOCIAL = "social"
    NEWS = "news"
    FUNDING_DATABASE = "funding_database"
    SEARCH = "search"
    JOB_BOARD = "job_board"
    DISCOVERY_ACTOR = "discovery_actor"
    OTHER = "other"


class SignalType(StrEnum):
    FUNDING = "funding"
    AI_INTEREST = "ai_interest"
    EXPANSION = "expansion"
    HIRING = "hiring"
    PAIN_POINT = "pain_point"
    TECHNOLOGY = "technology"
    PRODUCT_CUSTOMER = "product_customer"  # customers, partnerships, deployments (Phase 4)
    OTHER = "other"


class ResearchChannel(StrEnum):
    """Agent-Reach channels this project may use (Phase 4 policy: public + unauthenticated).

    The allow-list is enforced in code (``app.domain.signals.ALLOWED_CHANNELS``); settings can
    only pick a subset. LinkedIn, Instagram, X, Reddit, Facebook etc. are not representable.
    """

    NEWS_RSS = "news_rss"
    YOUTUBE = "youtube"


class ResearchRunStatus(StrEnum):
    SUCCEEDED = "succeeded"  # at least one signal stored
    EMPTY = "empty"  # ran, but no relevant items
    FAILED = "failed"
    SKIPPED = "skipped"  # e.g. company not VERIFIED


class GateDecision(StrEnum):
    """Final qualification gate output (blueprint §12)."""

    ENRICH = "enrich"
    REVIEW = "review"
    REJECT = "reject"


class ReviewStatus(StrEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"
    DECLINED = "declined"


class LayaDecisionKind(StrEnum):
    """Typed decision kinds supported by Laya: ``choice`` / ordinal ``score`` / ``noul`` (Laya's
    calibrated yes/no P(true), stored as PROBABILITY)."""

    CHOICE = "choice"
    SCORE = "score"
    PROBABILITY = "probability"


class QualificationDimension(StrEnum):
    FUNDING = "funding"
    COMPANY_FIT = "company_fit"
    AI_INTENT = "ai_intent"
    EXPANSION = "expansion"
    AUTOMATION_OPPORTUNITY = "automation_opportunity"
    FOUNDER_RELEVANCE = "founder_relevance"
    EVIDENCE_QUALITY = "evidence_quality"
    # Phase 5 Laya questions
    BUSINESS_TRIGGER = "business_trigger"  # choice: primary trigger / path
    ATTRIBUTION = "attribution"  # noul: the evidence is about THIS company (not a namesake)


class EnrichmentStatus(StrEnum):
    PENDING = "pending"
    SUCCESS = "success"
    PARTIAL = "partial"
    NOT_FOUND = "not_found"
    FAILED = "failed"
    SKIPPED = "skipped"


class EnrichmentTarget(StrEnum):
    PERSON = "person"
    COMPANY = "company"


class PhoneStatus(StrEnum):
    """Outcome of the OPTIONAL phone capability for one contact (never guessed)."""

    NOT_REQUESTED = "not_requested"  # capability off (default) - no provider was asked
    FOUND = "found"  # an identity-matched provider result carried a number
    NOT_FOUND = "not_found"  # asked, no provider returned one
    BUDGET_EXHAUSTED = "budget_exhausted"  # capability on, per-run phone budget used up


class EmailVerificationStatus(StrEnum):
    """Discovery is not verification: a found address starts as ``UNVERIFIED``."""

    UNVERIFIED = "unverified"
    VALID = "valid"
    INVALID = "invalid"
    ACCEPT_ALL = "accept_all"
    UNKNOWN = "unknown"
    WEBMAIL = "webmail"  # Hunter: free webmail (gmail etc.) - not a company address
    DISPOSABLE = "disposable"
    PENDING = "pending"  # Hunter HTTP 202: verification still running


class PipelineStage(StrEnum):
    DISCOVERY = "discovery"
    WEBSITE_INTELLIGENCE = "website_intelligence"
    SIGNAL_RESEARCH = "signal_research"
    NORMALIZATION = "normalization"
    QUALIFICATION = "qualification"
    ENRICHMENT = "enrichment"
    ANALYSIS = "analysis"
    EXPORT = "export"


class DomainResolutionStatus(StrEnum):
    """Official-website resolution state of a company (Phase 3)."""

    UNRESOLVED = "unresolved"  # never attempted
    RESOLVED = "resolved"  # confident candidate, not yet verified against the homepage
    VERIFIED = "verified"  # homepage confirms the company identity
    REVIEW = "review"  # ambiguous or failed verification: a human decides; never crawled/merged
    NOT_FOUND = "not_found"  # search returned no plausible official site
    FAILED = "failed"  # provider error during resolution


class PageType(StrEnum):
    """Website page kinds, in crawl priority order."""

    HOME = "home"
    ABOUT = "about"
    PRODUCTS = "products"
    CAREERS = "careers"
    PRICING = "pricing"
    BLOG = "blog"
    CONTACT = "contact"
    OTHER = "other"


class CrawlStatus(StrEnum):
    SUCCEEDED = "succeeded"  # sufficient content extracted
    INSUFFICIENT = "insufficient"  # ran but too little usable content
    FAILED = "failed"
    SKIPPED = "skipped"  # e.g. identity not verified / budget exhausted


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ------------------------------------------------------------------ Phase 5 qualification
class QualificationDecision(StrEnum):
    """Final qualification gate output (Phase 5). Only QUALIFIED may ever reach enrichment."""

    QUALIFIED = "qualified"
    REVIEW = "review"
    NOT_QUALIFIED = "not_qualified"


class RuleOutcome(StrEnum):
    """Hard-rule result. UNKNOWN (missing/conflicting data) never counts as a pass."""

    PASS = "pass"  # noqa: S105 - rule outcome, not a password
    FAIL = "fail"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"  # e.g. no funding evidence at all


class QualificationPath(StrEnum):
    FUNDING = "funding"
    INTENT = "intent"
    BOTH = "both"
    NONE = "none"


class ContactStatus(StrEnum):
    """Outcome for one person of one QUALIFIED company (Phase 6)."""

    VERIFIED = "verified"  # email found AND verification returned valid (fresh)
    UNVERIFIED = "unverified"  # email found, verification not valid (accept_all/unknown/...)
    INVALID_EMAIL = "invalid_email"  # verification said invalid/disposable - not usable
    NOT_FOUND = "not_found"  # providers returned no acceptable email
    NO_PERSON = "no_person"  # no senior decision-maker identified
    AMBIGUOUS = "ambiguous"  # identity could not be resolved safely - nothing called
    BUDGET_EXHAUSTED = "budget_exhausted"
    BLOCKED = "blocked"  # qualification gate refused - nothing called
    FAILED = "failed"


class AttemptStatus(StrEnum):
    HIT = "hit"
    MISS = "miss"
    ERROR = "error"
    SKIPPED = "skipped"  # e.g. budget exhausted before the call


class ProviderOperation(StrEnum):
    EMAIL_FINDER = "email_finder"  # Hunter (via enrichment-kit waterfall)
    PERSON_MATCH = "person_match"  # Apollo people/match (via enrichment-kit waterfall)
    EMAIL_VERIFIER = "email_verifier"  # Hunter
    DOMAIN_SEARCH = "domain_search"  # Hunter decision-maker discovery


class SourcePolicyState(StrEnum):
    """Permission state of a discovery source (Source Actor Registry)."""

    APPROVED = "approved"
    PENDING_REVIEW = "pending_review"
    BLOCKED = "blocked"


class StageStatus(StrEnum):
    """Status of an optional stage (Laya / LLM review) inside one decision."""

    OK = "ok"
    SKIPPED = "skipped"  # not needed (e.g. hard rules failed) or disabled
    ERROR = "error"
