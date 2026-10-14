"""Capability interfaces and shared adapter plumbing.

Business logic depends on the *capability* protocols below, never on a vendor class. Each vendor
package (``app/providers/<vendor>/``) ships two implementations:

* ``mock.py``   - deterministic, offline fake used in tests and ``PROVIDER_MODE=mock``.
* ``adapter.py`` - the live adapter. In Phase 1 every live method runs the safety guard and then
  raises ``ProviderNotImplementedError``; the real SDK/API call is added in the provider's phase
  without changing the protocol.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import ClassVar, NoReturn, Protocol, runtime_checkable

from app.core.config import PAID_PROVIDERS, Settings
from app.core.errors import (
    ProviderCallBlockedError,
    ProviderNotConfiguredError,
    ProviderNotImplementedError,
)
from app.core.logging import get_logger
from app.domain.enums import ProviderName
from app.domain.models import EnrichmentResult
from app.providers.contracts import (
    ChatRequest,
    ChatResult,
    DiscoveryQuery,
    DiscoveryResult,
    EmailVerificationResult,
    EnrichmentOutcome,
    EnrichmentRequest,
    LlmReviewRequest,
    LlmReviewResult,
    PeopleSearchRequest,
    PeopleSearchResult,
    ProviderStatus,
    QualificationRequest,
    QualificationResult,
    SignalResearchRequest,
    SignalResearchResult,
    WebSearchQuery,
    WebSearchResult,
    WebsiteExtractionRequest,
    WebsiteExtractionResult,
)

logger = get_logger(__name__)

# Called by a discovery provider as soon as the vendor job exists (e.g. with the Apify run id),
# so the caller can persist the id before waiting for the result (Phase 5 fix D1).
RunStartedHook = Callable[[str], Awaitable[None]]


# ------------------------------------------------------------------------ capability protocols
@runtime_checkable
class Provider(Protocol):
    name: ClassVar[ProviderName]

    def status(self) -> ProviderStatus:
        """Config-only status. Must never perform network I/O."""
        ...


@runtime_checkable
class DiscoveryProvider(Provider, Protocol):
    """Apify: source-specific discovery/collection via Actors."""

    async def discover(
        self, query: DiscoveryQuery, *, on_run_started: RunStartedHook | None = None
    ) -> DiscoveryResult: ...


@runtime_checkable
class WebSearchProvider(Provider, Protocol):
    """General web search (Firecrawl Search now; Brave or others can implement it later)."""

    async def search_web(self, query: WebSearchQuery) -> WebSearchResult: ...


@runtime_checkable
class WebsiteIntelligenceProvider(Provider, Protocol):
    """Firecrawl (primary) and Crawl4AI (fallback) share this interface."""

    async def extract_site(self, request: WebsiteExtractionRequest) -> WebsiteExtractionResult: ...


@runtime_checkable
class SignalResearchProvider(Provider, Protocol):
    """Agent-Reach: public social/web signal research around a company/founder."""

    async def research(self, request: SignalResearchRequest) -> SignalResearchResult: ...


@runtime_checkable
class QualificationProvider(Provider, Protocol):
    """Laya: fast typed fit decisions before any enrichment spend."""

    async def evaluate(self, request: QualificationRequest) -> QualificationResult: ...


@runtime_checkable
class ReasoningProvider(Provider, Protocol):
    """Techno-AI-Router chat completions - the single AI boundary for Phase 7 analysis."""

    async def complete(self, request: ChatRequest) -> ChatResult: ...


@runtime_checkable
class LlmReviewProvider(Provider, Protocol):
    """AI Router (hosted LLM): structured second opinion on REVIEW cases only (Phase 5)."""

    async def review(self, request: LlmReviewRequest) -> LlmReviewResult: ...


@runtime_checkable
class EnrichmentOrchestrator(Provider, Protocol):
    """enrichment-kit: runs the vendor waterfall; the only enrichment entry point for workflows.
    ``find_people`` discovers decision-makers on a verified domain through the same sidecar."""

    async def enrich(self, request: EnrichmentRequest) -> EnrichmentOutcome: ...

    async def find_people(self, request: PeopleSearchRequest) -> PeopleSearchResult: ...


@runtime_checkable
class EnrichmentVendor(Provider, Protocol):
    """A single enrichment source (Hunter, Apollo) usable inside the waterfall."""

    async def enrich(self, request: EnrichmentRequest) -> EnrichmentResult: ...


@runtime_checkable
class EmailVerifier(Provider, Protocol):
    """Hunter: verification is a separate step from discovery."""

    async def verify_email(self, email: str) -> EmailVerificationResult: ...


# ------------------------------------------------------------------------ shared base classes
class MockProviderBase:
    """Offline fake. Records calls so tests can assert on routing."""

    name: ClassVar[ProviderName]

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings
        self.calls: list[str] = []

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            name=self.name,
            mode="mock",
            configured=True,
            operational=True,
            paid=False,
            detail="deterministic offline mock",
        )


class LiveAdapterBase:
    """Live adapter skeleton with the credit/safety guard every real call must pass through."""

    name: ClassVar[ProviderName]
    implemented_in_phase: ClassVar[int]
    operational: ClassVar[bool] = False  # flipped to True when the real integration lands

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def paid(self) -> bool:
        return self.name in PAID_PROVIDERS

    def status(self) -> ProviderStatus:
        configured = self._settings.provider_has_credentials(self.name)
        detail = (
            f"contract only; live integration scheduled for Phase {self.implemented_in_phase}"
            if not self.operational
            else None
        )
        return ProviderStatus(
            name=self.name,
            mode="live",
            configured=configured,
            operational=self.operational and configured,
            paid=self.paid,
            detail=detail,
        )

    def _ensure_callable(self, operation: str) -> None:
        """Safety gate executed before any outbound call. Order: paid guard, then credentials."""
        if self.paid and not self._settings.allow_paid_provider_calls:
            logger.warning(
                "blocked paid provider call",
                extra={"provider": self.name.value, "operation": operation},
            )
            raise ProviderCallBlockedError(
                self.name.value,
                f"{operation} blocked: paid provider calls are disabled "
                "(set ALLOW_PAID_PROVIDER_CALLS=true only when intentionally spending credits)",
                details={"operation": operation},
            )
        if not self._settings.provider_has_credentials(self.name):
            raise ProviderNotConfiguredError(
                self.name.value,
                f"{operation} requires credentials for {self.name.value}; see PROVIDER_SETUP.md",
                details={"operation": operation},
            )

    def _not_implemented(self, operation: str) -> NoReturn:
        raise ProviderNotImplementedError(
            self.name.value,
            f"{self.name.value}.{operation} is a Phase 1 contract; "
            f"live integration is scheduled for Phase {self.implemented_in_phase}",
            details={"operation": operation, "phase": self.implemented_in_phase},
        )

    def _live_call_stub(self, operation: str) -> NoReturn:
        self._ensure_callable(operation)
        self._not_implemented(operation)
