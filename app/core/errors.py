"""Error conventions.

Every expected failure is an ``AppError`` subclass carrying a stable machine-readable ``code``,
a safe human message, optional structured ``details`` and the HTTP status the API should use.
Messages and details must never contain secrets. ``retryable`` tells future workers (Phase 9)
whether an automatic retry makes sense.
"""

from __future__ import annotations

from typing import Any, ClassVar


class AppError(Exception):
    code: ClassVar[str] = "app_error"
    http_status: ClassVar[int] = 500
    retryable: ClassVar[bool] = False

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class ConfigurationError(AppError):
    code = "configuration_error"
    http_status = 500


class DomainValidationError(AppError):
    code = "domain_validation_error"
    http_status = 422


class InvalidStateTransitionError(DomainValidationError):
    code = "invalid_state_transition"
    http_status = 409


class EnrichmentNotAllowedError(InvalidStateTransitionError):
    """Phase 5 gate: only the latest QUALIFIED decision may enter Phase 6 enrichment."""

    code = "enrichment_not_allowed"
    http_status = 409


class EnrichmentBudgetExceededError(AppError):
    """Phase 6: a configured enrichment budget (lead / run / month / sidecar) is exhausted."""

    code = "enrichment_budget_exceeded"
    http_status = 429


class SourceNotRunnableError(DomainValidationError):
    """Source Actor Registry: source unknown, disabled or not permitted by policy."""

    code = "source_not_runnable"
    http_status = 403


class NotFoundError(AppError):
    code = "not_found"
    http_status = 404


# --------------------------------------------------------------------- providers
class ProviderError(AppError):
    """Base for all external-provider failures. ``provider`` is always in details."""

    code = "provider_error"
    http_status = 502

    def __init__(
        self, provider: str, message: str, *, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message, details={"provider": provider, **(details or {})})
        self.provider = provider


class ProviderNotConfiguredError(ProviderError):
    code = "provider_not_configured"
    http_status = 503


class ProviderCallBlockedError(ProviderError):
    """Raised by the credit/safety guard before any paid or live call leaves the process."""

    code = "provider_call_blocked"
    http_status = 503


class ProviderNotImplementedError(ProviderError):
    """Live adapter exists as a contract only; the real integration lands in a later phase."""

    code = "provider_not_implemented"
    http_status = 501


class ProviderRunFailedError(ProviderError):
    """A provider job ran but did not succeed (e.g. Apify run FAILED / TIMED-OUT / ABORTED).

    ``details`` carries the provider run id, final status and any reported cost so the failure
    is auditable.
    """

    code = "provider_run_failed"
    http_status = 502


class ProviderUnavailableError(ProviderError):
    """Transient upstream failure (timeout, 5xx, rate limit). Safe to retry with backoff."""

    code = "provider_unavailable"
    http_status = 503
    retryable = True


# ------------------------------------------------------------ Phase 4 research failure modes
class ProviderTimeoutError(ProviderUnavailableError):
    code = "provider_timeout"


class ProviderRateLimitedError(ProviderUnavailableError):
    code = "provider_rate_limited"


class ProviderModelUnavailableError(ProviderError):
    """The requested model is not enabled / not allowed / not accepted by the provider."""

    code = "provider_model_unavailable"
    http_status = 503


class ProviderQuotaExhaustedError(ProviderError):
    """The provider account's own usage limit is used up (e.g. Hunter HTTP 429)."""

    code = "provider_quota_exhausted"
    http_status = 402


class ProviderMalformedResponseError(ProviderError):
    """Backend answered, but the payload could not be parsed into our contracts."""

    code = "provider_malformed_response"
    http_status = 502


class ProviderChannelUnsupportedError(ProviderError):
    """A channel the adapter does not implement (or whose backend is not installed)."""

    code = "provider_channel_unsupported"
    http_status = 501


class ProviderPolicyBlockedError(ProviderCallBlockedError):
    """Refused by source policy (e.g. LinkedIn/Instagram, login-only or cookie-based routes)."""

    code = "provider_policy_blocked"


class ProviderBudgetExceededError(ProviderCallBlockedError):
    """The call would exceed (or the provider already exceeded) a configured spend budget."""

    code = "provider_budget_exceeded"


class ProviderRefusalError(ProviderError):
    """The model declined to answer (safety refusal). Never retried."""

    code = "provider_refused"
    http_status = 502


class ProviderIncompleteResponseError(ProviderMalformedResponseError):
    """The model stopped before finishing (e.g. output-token limit); the partial text is
    unusable."""

    code = "provider_incomplete_response"
