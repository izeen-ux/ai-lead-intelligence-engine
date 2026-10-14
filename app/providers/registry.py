"""Single place that decides which adapter implementation backs each capability.

Business code asks the registry for a *capability* (``registry.discovery``), never for a vendor
class. Swapping a vendor later means changing one line here.
"""

from __future__ import annotations

from app.core.config import ProviderMode, ReasoningProviderChoice, Settings
from app.domain.enums import ProviderName
from app.providers.agent_reach import AgentReachAdapter, MockAgentReachProvider
from app.providers.ai_router import AiRouterAdapter, MockAiRouterProvider
from app.providers.anthropic import AnthropicAdapter
from app.providers.apify import ApifyAdapter, MockApifyProvider
from app.providers.apify.post_reader import PostReader
from app.providers.apollo import ApolloAdapter, MockApolloProvider
from app.providers.base import (
    DiscoveryProvider,
    EmailVerifier,
    EnrichmentOrchestrator,
    EnrichmentVendor,
    LlmReviewProvider,
    Provider,
    QualificationProvider,
    ReasoningProvider,
    SignalResearchProvider,
    WebSearchProvider,
    WebsiteIntelligenceProvider,
)
from app.providers.contracts import ProviderStatus
from app.providers.crawl4ai import Crawl4AIAdapter, MockCrawl4AIProvider
from app.providers.enrichment_kit import EnrichmentKitAdapter, MockEnrichmentKitProvider
from app.providers.firecrawl import FirecrawlAdapter, MockFirecrawlProvider
from app.providers.hunter import HunterAdapter, MockHunterProvider
from app.providers.laya import LayaAdapter, MockLayaProvider
from app.providers.openai import MockOpenAIProvider, OpenAIAdapter


def live_reasoning(settings: Settings) -> OpenAIAdapter | AiRouterAdapter | AnthropicAdapter:
    """The live reasoning adapter chosen with REASONING_PROVIDER: OpenAI (code default),
    Claude ("anthropic") or the deprecated router (explicit opt-in)."""
    if settings.reasoning_provider is ReasoningProviderChoice.AI_ROUTER:
        return AiRouterAdapter(settings)
    if settings.reasoning_provider is ReasoningProviderChoice.ANTHROPIC:
        return AnthropicAdapter(settings)
    return OpenAIAdapter(settings)


def mock_reasoning(settings: Settings | None) -> MockOpenAIProvider | MockAiRouterProvider:
    if settings is not None and settings.reasoning_provider is ReasoningProviderChoice.AI_ROUTER:
        return MockAiRouterProvider(settings)
    return MockOpenAIProvider(settings)


class ProviderRegistry:
    def __init__(self, settings: Settings) -> None:
        self.mode = settings.provider_mode
        live = self.mode == ProviderMode.LIVE

        reasoning = live_reasoning(settings) if live else mock_reasoning(settings)
        # AI post reader: the reasoning model names the companies in social posts the
        # patterns could not read (live only, DISCOVERY_LLM_EXTRACTION=true).
        reader = PostReader(reasoning).read if settings.discovery_llm_extraction else None
        self.discovery: DiscoveryProvider = (
            ApifyAdapter(settings, post_reader=reader) if live else MockApifyProvider(settings)
        )
        firecrawl = FirecrawlAdapter(settings) if live else MockFirecrawlProvider(settings)
        self.website: WebsiteIntelligenceProvider = firecrawl
        # General web search (Phase 3: Firecrawl Search; Brave or others can replace it later).
        self.web_search: WebSearchProvider = firecrawl
        self.website_fallback: WebsiteIntelligenceProvider = (
            Crawl4AIAdapter(settings) if live else MockCrawl4AIProvider(settings)
        )
        self.signal_research: SignalResearchProvider = (
            AgentReachAdapter(settings) if live else MockAgentReachProvider(settings)
        )
        self.qualification: QualificationProvider = (
            LayaAdapter(settings) if live else MockLayaProvider(settings)
        )
        self.enrichment: EnrichmentOrchestrator = (
            EnrichmentKitAdapter(settings) if live else MockEnrichmentKitProvider(settings)
        )
        hunter = HunterAdapter(settings) if live else MockHunterProvider(settings)
        self.email_vendor: EnrichmentVendor = hunter
        self.email_verifier: EmailVerifier = hunter
        self.people_vendor: EnrichmentVendor = (
            ApolloAdapter(settings) if live else MockApolloProvider(settings)
        )
        # Reasoning boundary (Phase 10): OpenAI. The deprecated Techno-AI-Router is used only
        # when REASONING_PROVIDER=ai_router is set explicitly - it is never called otherwise.
        reasoning = live_reasoning(settings) if live else mock_reasoning(settings)
        self.reasoning: ReasoningProvider = reasoning
        # REVIEW-case second opinion (Phase 5); needs LLM_REVIEW_ENABLED=true as well.
        self.llm_review: LlmReviewProvider = reasoning

    def all(self) -> dict[ProviderName, Provider]:
        providers: list[Provider] = [
            self.discovery,
            self.website,
            self.website_fallback,
            self.signal_research,
            self.qualification,
            self.enrichment,
            self.email_vendor,
            self.people_vendor,
            self.llm_review,
        ]
        return {p.name: p for p in providers}

    def statuses(self) -> list[ProviderStatus]:
        return [p.status() for p in self.all().values()]
