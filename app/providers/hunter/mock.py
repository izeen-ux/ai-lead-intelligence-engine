from __future__ import annotations

from app.domain.enums import EmailVerificationStatus, EnrichmentStatus, ProviderName
from app.domain.models import EnrichmentResult
from app.providers.base import MockProviderBase
from app.providers.contracts import EmailVerificationResult, EnrichmentRequest

# Verification outcome by the address's local-part prefix (deterministic, offline).
_BY_PREFIX = (
    ("invalid", EmailVerificationStatus.INVALID, None),
    ("acceptall", EmailVerificationStatus.ACCEPT_ALL, None),
    ("unknown", EmailVerificationStatus.UNKNOWN, None),
    ("pending", EmailVerificationStatus.PENDING, None),
    ("vtimeout", EmailVerificationStatus.UNKNOWN, "timeout"),
)


class MockHunterProvider(MockProviderBase):
    name = ProviderName.HUNTER

    def __init__(self, settings=None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(settings)
        self.verified: list[str] = []

    async def enrich(self, request: EnrichmentRequest) -> EnrichmentResult:
        self.calls.append("enrich")
        if not request.domain:
            return EnrichmentResult(
                lead_id=request.lead_id,
                target=request.target,
                provider=self.name,
                status=EnrichmentStatus.NOT_FOUND,
            )
        return EnrichmentResult(
            lead_id=request.lead_id,
            target=request.target,
            provider=self.name,
            status=EnrichmentStatus.SUCCESS,
            email=f"founder@{request.domain}",
            confidence=0.85,  # discovered, not yet verified
        )

    async def verify_email(self, email: str) -> EmailVerificationResult:
        self.calls.append("verify_email")
        self.verified.append(email)
        local = email.split("@", 1)[0].lower()
        for prefix, status, code in _BY_PREFIX:
            if local.startswith(prefix):
                return EmailVerificationResult(
                    provider=self.name, email=email, status=status, score=0.5, error_code=code
                )
        return EmailVerificationResult(
            provider=self.name, email=email, status=EmailVerificationStatus.VALID, score=0.95
        )
