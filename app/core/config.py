"""Typed application settings.

All configuration comes from environment variables (optionally a local ``.env`` file).
Secrets are typed as ``SecretStr`` so they never appear in ``repr()``, logs or tracebacks.
No credential is required to start the application: providers run in ``mock`` mode by default.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.enums import ProviderName, ResearchChannel


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class ProviderMode(StrEnum):
    """How provider adapters are resolved by the registry.

    ``mock``: deterministic in-process fakes; never touch the network (Phase 1 default).
    ``live``: real adapters. In Phase 1 these are non-operational stubs.
    """

    MOCK = "mock"
    LIVE = "live"


class LogFormat(StrEnum):
    JSON = "json"
    CONSOLE = "console"


# Which setting holds the credential each provider needs in live mode.
# Providers mapped to ``None`` need no secret (local library / self-hosted); their
# endpoint/model settings are validated in their own phase.
PROVIDER_CREDENTIAL_FIELDS: dict[ProviderName, str | None] = {
    ProviderName.APIFY: "apify_token",
    ProviderName.FIRECRAWL: "firecrawl_api_key",
    ProviderName.AGENT_REACH: None,
    ProviderName.CRAWL4AI: None,
    ProviderName.LAYA: None,
    ProviderName.ENRICHMENT_KIT: None,
    ProviderName.HUNTER: "hunter_api_key",
    ProviderName.APOLLO: "apollo_api_key",
    ProviderName.AI_ROUTER: "ai_router_api_key",
    ProviderName.OPENAI: "openai_api_key",
    ProviderName.ANTHROPIC: "anthropic_api_key",
}

# Providers whose calls consume paid credits. Guarded by ``allow_paid_provider_calls``.
# enrichment-kit is included because it spends Hunter/Apollo credits on our behalf.
PAID_PROVIDERS: frozenset[ProviderName] = frozenset(
    {
        ProviderName.APIFY,
        ProviderName.FIRECRAWL,
        ProviderName.ENRICHMENT_KIT,
        ProviderName.HUNTER,
        ProviderName.APOLLO,
        ProviderName.AI_ROUTER,  # hosted LLM calls cost money
        ProviderName.OPENAI,  # paid per token
        ProviderName.ANTHROPIC,  # paid per token
    }
)


class ReasoningProviderChoice(StrEnum):
    OPENAI = "openai"  # default since Phase 10
    ANTHROPIC = "anthropic"  # Claude - what the owner's .env selects since 2026-10-14
    AI_ROUTER = "ai_router"  # deprecated Techno-AI-Router: explicit opt-in only


class LayaAmbiguousPolicy(StrEnum):
    """What an AMBIGUOUS Laya answer does to a company whose hard ICP rules pass."""

    QUALIFY = "qualify"  # default: hard rules decide; Laya only vetoes (NEGATIVE)
    REVIEW = "review"  # a human decides every ambiguous case


class OverCapFundingPolicy(StrEnum):
    """What a recent funding round ABOVE the cap does to a company qualifying via the intent
    path (the paths are independent; this only decides how cautious to be)."""

    IGNORE = "ignore"  # fully independent paths
    REVIEW = "review"  # default: intent-qualified but recently raised above cap -> human decides
    REJECT = "reject"  # treat an above-cap recent round as outside the ICP


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application -------------------------------------------------------
    app_name: str = "ai-lead-intelligence"
    app_env: Environment = Environment.DEVELOPMENT
    debug: bool = False
    log_level: str = Field(default="INFO", pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    log_format: LogFormat = LogFormat.JSON
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)

    # --- Database (PostgreSQL, system of record) ----------------------------
    # e.g. postgresql+psycopg://user:password@localhost:5432/lead_intel
    database_url: SecretStr | None = None
    database_echo: bool = False
    database_connect_timeout_seconds: int = Field(default=3, ge=1, le=60)

    # --- Provider safety switches ------------------------------------------
    provider_mode: ProviderMode = ProviderMode.MOCK
    allow_paid_provider_calls: bool = False

    # --- LLM / reasoning: OpenAI (Phase 10, active) --------------------------
    # Which adapter backs the ReasoningProvider capability. "ai_router" is the deprecated
    # Techno-AI-Router and is never selected unless set explicitly here.
    reasoning_provider: ReasoningProviderChoice = ReasoningProviderChoice.OPENAI
    openai_api_key: SecretStr | None = None
    # Verified against the live catalog + pricing page on 2026-10-05
    # (docs/OPENAI_PROVIDER_NOTES.md).
    openai_model: str = Field(default="gpt-6-luna", min_length=1, max_length=100)
    openai_base_url: str | None = None  # empty = the SDK's default endpoint
    openai_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    openai_max_retries: int = Field(default=2, ge=0, le=5)  # transient errors only
    openai_max_tokens: int = Field(default=2500, ge=64, le=16000)  # max_output_tokens
    openai_reasoning_effort: str = Field(default="", pattern=r"^(|none|minimal|low|medium|high)$")
    # USD per 1M tokens. Empty = built-in table for known models; unknown model + empty = refuse.
    openai_price_input_per_1m_usd: Decimal | None = Field(default=None, ge=0)
    openai_price_output_per_1m_usd: Decimal | None = Field(default=None, ge=0)
    # AI budgets (estimated from token usage x price). Checked before every call.
    openai_max_tokens_per_run: int = Field(default=60000, ge=1000, le=5_000_000)
    openai_max_estimated_cost_usd_per_run: Decimal = Field(default=Decimal("0.25"), ge=0)
    openai_max_estimated_cost_usd_per_month: Decimal = Field(default=Decimal("5.00"), ge=0)

    # --- LLM / reasoning: Claude (REASONING_PROVIDER=anthropic) ---------------
    anthropic_api_key: SecretStr | None = None  # a Claude API key starts with "sk-ant-"
    anthropic_model: str = Field(default="claude-opus-5-5", min_length=1, max_length=100)
    # Thinking depth / token spend of one answer (the model's own default is "medium").
    anthropic_effort: str = Field(default="medium", pattern=r"^(low|medium|high|xhigh|max)$")
    anthropic_max_tokens: int = Field(default=16000, ge=256, le=64000)  # thinking + answer
    anthropic_timeout_seconds: float = Field(default=180.0, gt=0, le=600)
    anthropic_max_retries: int = Field(default=2, ge=0, le=5)  # transient errors only
    # A policy refusal is retried server-side on Anthropic's recommended fallback model.
    anthropic_refusal_fallback: bool = True
    # Empty = the built-in price of the configured model (app/providers/anthropic/adapter.py).
    anthropic_price_input_per_1m_usd: Decimal | None = Field(default=None, ge=0)
    anthropic_price_output_per_1m_usd: Decimal | None = Field(default=None, ge=0)
    anthropic_max_tokens_per_run: int = Field(default=200000, ge=1000, le=5_000_000)
    anthropic_max_estimated_cost_usd_per_run: Decimal = Field(default=Decimal("1.00"), ge=0)
    anthropic_max_estimated_cost_usd_per_month: Decimal = Field(default=Decimal("15.00"), ge=0)
    # Ask the reasoning model to name the companies in social posts (Reddit, X, LinkedIn)
    # where the pattern-based extraction found none.
    discovery_llm_extraction: bool = False

    # --- DEPRECATED: Techno-AI-Router (legacy; REASONING_PROVIDER=ai_router) --
    # Techno-AI-Router (Phase 7): the single AI boundary. Discovery endpoints are open; chat
    # needs AI_ROUTER_API_KEY (a tair_... key, sent as Authorization: Bearer).
    ai_router_base_url: str | None = "https://techno-ai-router.vercel.app"  # documented base
    ai_router_api_key: SecretStr | None = None
    ai_router_default_model: str | None = None  # empty = first enabled model under the ceiling
    ai_router_gateway: str = Field(default="openrouter", pattern=r"^[a-z0-9_-]{2,40}$")
    ai_router_fallback_models: str = ""  # comma list of "gateway/model" (router fallback array)
    ai_router_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    ai_router_max_retries: int = Field(default=1, ge=0, le=5)
    ai_router_max_tokens: int = Field(default=1500, ge=64, le=16000)  # router field maxTokens
    ai_router_temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    # Cost ceiling per model (USD per 1M tokens, in AND out). 0 = only $0 / $0 (free) models.
    ai_router_max_price_per_1m_tokens_usd: Decimal = Field(default=Decimal(0), ge=0)
    # Phase 7 analysis budgets / eligibility
    analysis_max_per_run: int = Field(default=5, ge=1, le=100)
    analysis_max_tokens_per_run: int = Field(default=30000, ge=1000, le=2_000_000)
    analysis_max_cost_usd_per_run: Decimal = Field(default=Decimal(0), ge=0)
    analysis_max_cost_usd_per_month: Decimal = Field(default=Decimal(0), ge=0)
    analysis_require_verified_contact: bool = True
    analysis_min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    # --- Pipeline orchestration (Phase 9) ------------------------------------
    # Stages a pipeline run executes, in the fixed order discovery -> ... -> export.
    # Discovery is opt-in (it spends Apify credit): add "discovery" here or pass
    # ``--stages all``. Its queries come from DISCOVERY_QUERIES / DISCOVERY_NEWS_QUERIES.
    pipeline_stages: str = "prescreen,website,signals,qualification,enrichment,analysis,export"
    # Team members who may own leads. A pipeline run started with an owner assigns every lead
    # it produces to that person (comma list; empty = any name is accepted).
    lead_owners: str = ""  # comma list of team members; empty = owner is optional
    # Pre-screen (free) before website intelligence (paid): with PRESCREEN_REQUIRED the website
    # stage only processes companies that got a SCRAPE verdict. PRESCREEN_USE_LAYA adds the
    # local Laya veto on top of the deterministic rules.
    prescreen_required: bool = True
    prescreen_use_laya: bool = True
    # Deprecated override: comma list replacing DISCOVERY_QUERIES for the funding_news source.
    pipeline_discovery_keywords: str = ""
    # Companies per stage per run when the operator gives no --limit (conservative default).
    pipeline_company_limit: int = Field(default=5, ge=1, le=50)
    # Ceiling for an EXPLICIT --limit / API limit. A larger request is refused, never
    # silently reduced.
    pipeline_max_company_limit: int = Field(default=25, ge=1, le=50)
    pipeline_max_estimated_cost_usd_per_run: Decimal = Field(default=Decimal("1.00"), ge=0)
    pipeline_stale_after_minutes: int = Field(default=60, ge=5, le=1440)
    # "Leads wanted" runs: the stages repeat (next search queries, next companies) until the
    # wanted number of new sheet rows exists - at most this many rounds, inside the cost cap.
    pipeline_max_rounds: int = Field(default=5, ge=1, le=10)
    # Speed only - the same searches and checks run, several at a time instead of one by one.
    pipeline_parallel_sources: int = Field(default=4, ge=1, le=8)  # discovery sources at once
    pipeline_parallel_companies: int = Field(default=3, ge=1, le=8)  # websites scraped at once
    pipeline_max_target_leads: int = Field(default=10, ge=1, le=50)
    # Scheduler (scripts/run_scheduler.py): 0 = no automatic runs.
    pipeline_schedule_interval_minutes: int = Field(default=0, ge=0, le=10080)

    # --- Private service network (Phase 10) -----------------------------------
    # Local sidecars (Laya, enrichment-kit) must be reached on loopback. In a container
    # deployment they live on a PRIVATE Docker network instead: list their service names here
    # (comma). Never put a public hostname in this list.
    internal_service_hosts: str = ""
    # Container deployments: replace only the HOST of DATABASE_URL (e.g. "postgres").
    database_host_override: str | None = None
    # Use another database on the same server (e.g. the labelled demo database
    # "lead_intel_demo"); empty = the database named in DATABASE_URL.
    database_name_override: str | None = Field(default=None, pattern=r"^[a-z_][a-z0-9_]{0,62}$")

    # --- API hardening (Phase 10) ---------------------------------------------
    # Secret shared with the operator(s). Required for every /api/v1 call when set, and
    # mandatory when APP_ENV is staging/production.
    operator_api_token: SecretStr | None = None
    cors_allowed_origins: str = ""  # comma list of exact origins; empty = same-origin only
    max_request_body_bytes: int = Field(default=65536, ge=1024, le=1_048_576)

    # --- Outputs: Google Sheets projection (Phase 8) -------------------------
    # PostgreSQL stays the system of record; the Sheet is a read-mostly projection.
    # Service-account key FILE path (contents never read into settings, never logged).
    google_service_account_json: Path = Path("secrets/google/service-account.json")
    google_sheets_spreadsheet_id: str | None = None  # shown masked only
    # The ONE worksheet the app writes: qualified leads and leads waiting for review, told
    # apart by the ``lead_status`` column. Run history stays in PostgreSQL / the dashboard.
    google_sheets_worksheet: str = Field(default="Ai Leads", min_length=1, max_length=100)
    google_sheets_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    google_sheets_max_retries: int = Field(default=2, ge=0, le=5)
    export_max_rows_per_run: int = Field(default=50, ge=1, le=500)
    # A worksheet holds this many leads; the next lead starts a new tab ("Ai Leads 2", ...).
    # A lead always stays in the tab it was first written to.
    export_rows_per_sheet: int = Field(default=500, ge=1, le=10000)
    # Live Sheet writes need this AND the CLI --live flag (process-scoped, like paid calls).
    allow_sheets_writes: bool = False

    # --- Provider credentials / endpoints (all optional in Phase 1) ---------
    apify_token: SecretStr | None = None
    # Backup keys (comma-separated). When a key is rejected or out of credits the next one is
    # used automatically (app/core/keyring.py). Same for Firecrawl, Hunter and Apollo.
    apify_token_backups: SecretStr | None = None
    # Discovery run controls (Phase 2). These are hard ceilings: a request may ask for less,
    # never more. One dataset item of the default Actor = one Google results page (~10 results).
    apify_discovery_actor_id: str = "apify/google-search-scraper"
    apify_max_items_per_run: int = Field(default=1, ge=1, le=20)
    # Hard cap enforced by Apify itself (``max_total_charge_usd``). Apify rejects values below
    # $0.50 (verified 2026-09-28), so this is a backstop, not the tight budget control.
    apify_max_charge_usd_per_run: Decimal = Field(
        default=Decimal("0.50"), ge=Decimal("0.50"), le=Decimal(5)
    )
    # Tight budget control: the Actor spec's worst-case cost estimate for the exact input must
    # not exceed this, otherwise the run is blocked before Apify is called.
    apify_max_estimated_cost_usd_per_run: Decimal = Field(
        default=Decimal("0.05"), gt=0, le=Decimal(5)
    )
    apify_run_timeout_seconds: int = Field(default=180, ge=10, le=3600)
    firecrawl_api_key: SecretStr | None = None
    firecrawl_api_key_backups: SecretStr | None = None
    firecrawl_base_url: str | None = None  # empty = hosted API https://api.firecrawl.dev/v2
    firecrawl_timeout_seconds: int = Field(default=60, ge=5, le=300)
    # Website-intelligence budgets (Phase 3). Hard ceilings; requests can only lower them.
    website_max_pages_per_company: int = Field(default=8, ge=1, le=20)
    # Worst-case Firecrawl credits for one company = search (2 per 10 results) + map (1)
    # + one per scraped page. Blocked before any call if it would exceed this.
    firecrawl_max_credits_per_company: int = Field(default=15, ge=1, le=200)
    firecrawl_max_credits_per_run: int = Field(default=50, ge=1, le=5000)
    domain_search_result_limit: int = Field(default=5, ge=1, le=10)
    domain_min_confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    # Crawl4AI fallback: official Docker server (bound to 127.0.0.1). Keyless provider;
    # the token only authenticates us to our own local container.
    crawl4ai_base_url: str | None = None  # e.g. http://127.0.0.1:11235
    crawl4ai_api_token: SecretStr | None = None
    crawl4ai_timeout_seconds: int = Field(default=90, ge=5, le=600)
    # Agent-Reach signal research (Phase 4). Public, unauthenticated channels only; the allowed
    # set is fixed in code (app.domain.signals.ALLOWED_CHANNELS) - this picks a subset.
    agent_reach_channels: str = "news_rss,youtube"
    agent_reach_ytdlp_path: str = "yt-dlp"  # installed by Agent-Reach (uv tool, isolated)
    agent_reach_timeout_seconds: int = Field(default=30, ge=5, le=300)
    signal_max_queries_per_company: int = Field(default=5, ge=1, le=10)
    signal_max_items_per_company: int = Field(default=20, ge=1, le=100)
    # Laya (Phase 5): self-hosted `laya-serve` sidecar bound to localhost (isolated uv tool,
    # never in this app's dependency set). In-process checkpoints (LAYA_MODEL_PATH) are not
    # supported - they would pull torch/transformers into the app (ADR-040).
    laya_model_path: str | None = None  # reserved; not used by the live adapter
    laya_endpoint_url: str | None = None  # e.g. http://127.0.0.1:8765
    laya_api_key: SecretStr | None = None  # optional bearer token of our own sidecar
    laya_checkpoint: str = "typed-decisions"  # informational; the sidecar decides routing
    laya_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    # Phase 6 enrichment. enrichment-kit runs in a project-owned Node sidecar bound to
    # 127.0.0.1 (sidecars/enrichment-kit); Hunter/Apollo keys are read by the SIDECAR from .env,
    # this app only checks that they are present. Every paid call goes through the sidecar.
    enrichment_kit_base_url: str | None = None  # e.g. http://127.0.0.1:8787 (loopback only)
    enrichment_kit_api_token: SecretStr | None = None  # shared local bearer for the sidecar
    enrichment_kit_timeout_seconds: float = Field(default=45.0, gt=0, le=300)
    # Read by the SIDECAR (listen port, per-process hard cap on provider calls); declared here so
    # every .env key is typed and documented.
    enrichment_kit_port: int = Field(default=8787, ge=1, le=65535)
    enrichment_kit_max_provider_calls: int = Field(default=50, ge=1, le=10000)
    enrichment_min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    enrichment_early_exit_confidence: float = Field(default=0.85, ge=0.0, le=1.0)
    enrichment_accept_confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    enrichment_cache_ttl_days: int = Field(default=30, ge=1, le=365)
    enrichment_verification_ttl_days: int = Field(default=30, ge=1, le=365)
    enrichment_role_priority: str = (
        "founder,co-founder,ceo,chief executive,managing director,cto,owner,coo,president"
    )
    enrichment_max_persons_per_company: int = Field(default=1, ge=1, le=5)
    enrichment_people_search: str = Field(default="none", pattern=r"^(none|hunter)$")
    enrichment_max_attempts_per_lead: int = Field(default=4, ge=1, le=10)
    enrichment_max_sidecar_calls_per_run: int = Field(default=10, ge=1, le=500)
    # Phone capability: OFF by default. When on, the email-finder response may carry a
    # number the provider already published (no extra credit); Apollo mobile reveal (paid,
    # asynchronous) is never requested. Its own budget: contacts asked for a phone per run.
    # No founder on the company's own pages (and none from the people search): look the
    # founder up in public web-search results (title + snippet only), then find the email.
    # Each lookup is one web search (Firecrawl: 2 credits); own per-run budget.
    enrichment_web_search: bool = True
    enrichment_max_web_searches_per_run: int = Field(default=5, ge=0, le=100)
    enrichment_phone_enabled: bool = False
    enrichment_max_phone_lookups_per_run: int = Field(default=5, ge=0, le=500)
    enrichment_max_hunter_credits_per_run: int = Field(default=5, ge=0, le=1000)
    enrichment_max_apollo_credits_per_run: int = Field(default=3, ge=0, le=1000)
    enrichment_max_hunter_credits_per_month: int = Field(default=50, ge=0, le=100000)
    enrichment_max_apollo_credits_per_month: int = Field(default=20, ge=0, le=100000)
    # USD estimates per credit (plan-dependent; used for reporting + the USD budgets).
    enrichment_hunter_credit_usd: Decimal = Field(default=Decimal("0.03"), ge=0)
    enrichment_apollo_credit_usd: Decimal = Field(default=Decimal("0.05"), ge=0)
    enrichment_max_cost_usd_per_run: Decimal = Field(default=Decimal("0.50"), ge=0)
    enrichment_max_cost_usd_per_month: Decimal = Field(default=Decimal("5.00"), ge=0)
    # Source Actor Registry: sources to ENABLE (only approved, unauthenticated ones can run).
    discovery_sources_enabled: str = "funding_news"
    # ICP discovery query sets (";"-separated Google queries, one results page each). A run
    # uses as many queries as APIFY_MAX_ITEMS_PER_RUN and the Apify estimate budget allow,
    # rotating through the set from run to run.
    discovery_queries: str = (
        "startup raises seed funding crore India;"
        "Indian startup raises pre-seed round lakh;"
        "startup bags angel funding Rs crore;"
        "D2C brand raises seed funding India;"
        "SaaS startup secures pre-seed funding India;"
        "startup raises bridge round crore India"
    )
    # Social / job-platform sources (public Apify Actors, no login or cookies). One query per
    # source per run (rotating); DISCOVERY_SOCIAL_MAX_ITEMS_PER_RUN posts / jobs per source.
    discovery_linkedin_queries: str = (
        "startup raised seed round India;"
        "raised pre-seed funding India startup;"
        "angel round crore startup India"
    )
    discovery_linkedin_jobs_queries: str = (
        "founding engineer startup;first sales hire startup;founder's office"
    )
    discovery_x_queries: str = (
        "startup raises seed funding India;raises pre-seed round crore;bags angel funding crore"
    )
    # Reddit: "<subreddit>:<keywords>" entries - one subreddit search per run.
    discovery_reddit_queries: str = (
        "StartUpIndia:raised seed funding;indianstartups:raised funding;"
        "IndiaBusiness:startup raises crore"
    )
    discovery_social_max_items_per_run: int = Field(default=10, ge=1, le=100)
    discovery_news_queries: str = (
        "Indian startup launches new platform;"
        "India startup expands to new cities;"
        "bootstrapped Indian startup hiring team"
    )
    # Google "past N" date filter for discovery (m3 = 3 months = the 90-day trigger window);
    # empty = no date filter.
    discovery_quick_date_range: str = Field(default="m3", pattern=r"^([dwmy]\d{1,2})?$")
    hunter_api_key: SecretStr | None = None
    hunter_api_key_backups: SecretStr | None = None  # used by the enrichment sidecar
    apollo_api_key: SecretStr | None = None
    apollo_api_key_backups: SecretStr | None = None  # used by the enrichment sidecar

    # --- Business rules (ICP) ------------------------------------------------
    funding_cap_inr: Decimal = Field(default=Decimal("50000000"), gt=0)  # ₹5 Cr
    monthly_lead_target_min: int = Field(default=25, ge=1)
    monthly_lead_target_max: int = Field(default=30, ge=1)
    # Phase 5 ICP hard rules (read ONLY through app.domain.qualification.icp.IcpConfig).
    icp_country_code: str = Field(default="IN", pattern=r"^[A-Z]{2}$")
    icp_country_tlds: str = "in"  # ccTLDs accepted as geography evidence when country unknown
    # Optional FX for USD-reported rounds. Unset = USD amounts are "unknown" for the INR cap.
    icp_usd_to_inr_rate: Decimal | None = Field(default=None, gt=0)
    icp_funding_window_days: int = Field(default=90, ge=1, le=730)
    icp_intent_window_days: int = Field(default=90, ge=1, le=730)
    icp_over_cap_funding_policy: OverCapFundingPolicy = OverCapFundingPolicy.REVIEW
    icp_min_signal_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    # Event clustering (several publishers covering one event count once).
    cluster_title_similarity: float = Field(default=0.5, gt=0.0, le=1.0)
    cluster_window_days: int = Field(default=7, ge=0, le=60)
    # Laya decision thresholds (bounded probabilities).
    laya_qualify_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    laya_reject_threshold: float = Field(default=0.3, ge=0.0, le=1.0)
    # Hard rules pass + Laya neither clearly positive nor negative: qualify | review.
    # (Live data 2026-10-05: Laya rated every real company ambiguous, so "review" meant no
    # lead ever qualified automatically. Laya NEGATIVE still rejects; caps still force REVIEW.)
    icp_laya_ambiguous_policy: LayaAmbiguousPolicy = LayaAmbiguousPolicy.QUALIFY
    # Optional LLM review of REVIEW cases only (AI Router). Off by default.
    llm_review_enabled: bool = False
    llm_review_min_confidence: float = Field(default=0.75, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_consistency(self) -> Self:
        if self.monthly_lead_target_min > self.monthly_lead_target_max:
            raise ValueError("MONTHLY_LEAD_TARGET_MIN must be <= MONTHLY_LEAD_TARGET_MAX")
        if self.laya_reject_threshold >= self.laya_qualify_threshold:
            raise ValueError("LAYA_REJECT_THRESHOLD must be < LAYA_QUALIFY_THRESHOLD")
        if self.enrichment_min_confidence > self.enrichment_early_exit_confidence:
            raise ValueError(
                "ENRICHMENT_MIN_CONFIDENCE must be <= ENRICHMENT_EARLY_EXIT_CONFIDENCE"
            )
        if self.app_env is Environment.PRODUCTION:
            if self.debug:
                raise ValueError("DEBUG must be false in production")
            if self.database_url is None:
                raise ValueError("DATABASE_URL is required in production")
        return self

    @field_validator(
        "openai_base_url",
        "database_host_override",
        "database_name_override",
        "openai_price_input_per_1m_usd",
        "openai_price_output_per_1m_usd",
        "anthropic_price_input_per_1m_usd",
        "anthropic_price_output_per_1m_usd",
        mode="before",
    )
    @classmethod
    def _blank_openai_value_is_default(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("icp_usd_to_inr_rate", mode="before")
    @classmethod
    def _blank_is_none(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("ai_router_base_url", mode="before")
    @classmethod
    def _blank_router_url_is_default(cls, v: object) -> object:
        # an empty AI_ROUTER_BASE_URL= line in .env means "use the documented router"
        return "https://techno-ai-router.vercel.app" if isinstance(v, str) and not v.strip() else v

    @field_validator("agent_reach_channels")
    @classmethod
    def _allowed_channels(cls, v: str) -> str:
        allowed = {c.value for c in ResearchChannel}
        chosen = [c.strip().lower() for c in v.split(",") if c.strip()]
        bad = [c for c in chosen if c not in allowed]
        if bad:
            raise ValueError(
                f"AGENT_REACH_CHANNELS contains non-permitted channels {bad}; allowed: "
                f"{sorted(allowed)} (public, unauthenticated only)"
            )
        return ",".join(dict.fromkeys(chosen))

    def research_channels(self) -> list[ResearchChannel]:
        return [ResearchChannel(c) for c in self.agent_reach_channels.split(",") if c]

    # --- Helpers -------------------------------------------------------------
    def provider_credential(self, provider: ProviderName) -> SecretStr | None:
        field = PROVIDER_CREDENTIAL_FIELDS[provider]
        if field is None:
            return None
        value = getattr(self, field)
        return value if isinstance(value, SecretStr) else None

    def provider_has_credentials(self, provider: ProviderName) -> bool:
        """True when the provider needs no secret or its secret is set and non-blank."""
        if PROVIDER_CREDENTIAL_FIELDS[provider] is None:
            return True
        secret = self.provider_credential(provider)
        return secret is not None and bool(secret.get_secret_value().strip())

    def missing_provider_credentials(self) -> list[ProviderName]:
        """Providers in use whose secret is missing. The reasoning provider that is NOT
        selected (e.g. the deprecated AI router) is never required."""
        selected = ProviderName(self.reasoning_provider.value)
        reasoning = {ProviderName.OPENAI, ProviderName.AI_ROUTER, ProviderName.ANTHROPIC}
        unused = reasoning - {selected}
        return [p for p in ProviderName if p not in unused and not self.provider_has_credentials(p)]

    def provider_keys(self, field: str) -> list[str]:
        """Primary key of ``field`` (e.g. "apify_token") followed by its backups, in order."""
        from app.core.keyring import split_keys

        primary: SecretStr | None = getattr(self, field, None)
        backups: SecretStr | None = getattr(self, f"{field}_backups", None)
        return split_keys(
            primary.get_secret_value() if primary else None,
            backups.get_secret_value() if backups else None,
        )

    def secret_values(self) -> list[str]:
        """Plain values of every configured secret, for the log redaction filter only."""
        values = [
            part.strip()  # a "..._BACKUPS" setting holds several keys: redact each one
            for v in self.__dict__.values()
            if isinstance(v, SecretStr) and v.get_secret_value()
            for part in (v.get_secret_value(), *v.get_secret_value().split(","))
            if part.strip()
        ]
        if self.google_sheets_spreadsheet_id:  # not a credential, but never logged in full
            values.append(self.google_sheets_spreadsheet_id)
        return values

    @property
    def sheets_spreadsheet_ref(self) -> str | None:
        """Masked spreadsheet id for reports/history (last 6 characters only)."""
        sid = (self.google_sheets_spreadsheet_id or "").strip()
        return f"...{sid[-6:]}" if sid else None

    def is_private_service_host(self, host: str | None) -> bool:
        """True for loopback, or a service name explicitly listed in INTERNAL_SERVICE_HOSTS."""
        name = (host or "").lower()
        allowed = {h.strip().lower() for h in self.internal_service_hosts.split(",") if h.strip()}
        return name in {"127.0.0.1", "localhost", "::1"} or name in allowed

    @property
    def effective_database_url(self) -> str | None:
        """DATABASE_URL with DATABASE_HOST_OVERRIDE applied (the password is never logged)."""
        if self.database_url is None or not self.database_url.get_secret_value():
            return None
        url = self.database_url.get_secret_value()
        host = (self.database_host_override or "").strip()
        name = (self.database_name_override or "").strip()
        if not host and not name:
            return url
        from sqlalchemy.engine import make_url

        parsed = make_url(url)
        if host:
            parsed = parsed.set(host=host)
        if name:
            parsed = parsed.set(database=name)
        return parsed.render_as_string(hide_password=False)

    @property
    def database_name(self) -> str | None:
        """Name of the database in use (after overrides); None when not configured."""
        url = self.effective_database_url
        if url is None:
            return None
        from sqlalchemy.engine import make_url

        return make_url(url).database

    @property
    def database_configured(self) -> bool:
        return self.database_url is not None and bool(self.database_url.get_secret_value())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton. Tests call ``get_settings.cache_clear()``."""
    return Settings()
