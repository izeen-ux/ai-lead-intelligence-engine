"""Persistable enrichment records (Phase 6). Contacts and attempts are append-only."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from app.domain.enums import (
    AttemptStatus,
    ContactStatus,
    EmailVerificationStatus,
    PhoneStatus,
    ProviderName,
    ProviderOperation,
    RunStatus,
)
from app.domain.models import Entity, utcnow


class EnrichmentRunRecord(Entity):
    status: RunStatus = RunStatus.RUNNING
    mode: str  # mock | live
    policy_version: str
    config: dict[str, Any] = Field(default_factory=dict)
    companies_evaluated: int = Field(default=0, ge=0)
    contacts_created: int = Field(default=0, ge=0)
    verified_count: int = Field(default=0, ge=0)
    cached_count: int = Field(default=0, ge=0)
    blocked_count: int = Field(default=0, ge=0)
    failed_count: int = Field(default=0, ge=0)
    hunter_credits: Decimal = Field(default=Decimal(0), ge=0)
    apollo_credits: Decimal = Field(default=Decimal(0), ge=0)
    sidecar_calls: int = Field(default=0, ge=0)
    estimated_cost_usd: Decimal = Field(default=Decimal(0), ge=0)
    error_code: str | None = None
    error_message: str | None = None
    started_at: AwareDatetime = Field(default_factory=utcnow)
    finished_at: AwareDatetime | None = None


class EnrichmentContactRecord(Entity):
    """One person outcome (or a company-level BLOCKED/NO_PERSON/AMBIGUOUS outcome)."""

    run_id: UUID
    company_id: UUID
    company_name: str
    domain: str | None
    qualification_id: UUID | None  # the QUALIFIED decision that authorised it
    person_key: str | None = None
    full_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    role: str | None = None
    role_rank: int | None = None
    linkedin_url: str | None = None
    phone: str | None = None  # optional capability; never guessed
    phone_status: PhoneStatus = PhoneStatus.NOT_REQUESTED
    email: str | None = None
    email_verification: EmailVerificationStatus = EmailVerificationStatus.UNVERIFIED
    verification_provider: ProviderName | None = None
    verified_at: AwareDatetime | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    status: ContactStatus
    provider: ProviderName | None = None  # source of the email
    from_cache: bool = False
    identity: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    credits_used: dict[str, str] = Field(default_factory=dict)
    estimated_cost_usd: Decimal = Field(default=Decimal(0), ge=0)
    policy_version: str

    @model_validator(mode="after")
    def _invariants(self) -> Self:
        if self.status is ContactStatus.VERIFIED and (
            self.email is None or self.email_verification is not EmailVerificationStatus.VALID
        ):
            raise ValueError("VERIFIED contact needs an email with a VALID verification")
        if (self.phone is not None) != (self.phone_status is PhoneStatus.FOUND):
            raise ValueError("phone and phone_status FOUND go together")
        if self.email is None and self.email_verification is not EmailVerificationStatus.UNVERIFIED:
            raise ValueError("verification state requires an email")
        called = self.status not in (
            ContactStatus.BLOCKED, ContactStatus.NO_PERSON, ContactStatus.AMBIGUOUS
        )  # fmt: skip
        if called and self.qualification_id is None:
            raise ValueError("an enriched contact must reference its QUALIFIED decision")
        return self


class ProviderAttemptRecord(Entity):
    """One provider operation (append-only; the monthly budget is summed from these)."""

    run_id: UUID
    company_id: UUID
    person_key: str | None
    provider: ProviderName
    operation: ProviderOperation
    status: AttemptStatus
    error_code: str | None = None
    http_status: int | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    credits: Decimal = Field(default=Decimal(0), ge=0)
    estimated_cost_usd: Decimal = Field(default=Decimal(0), ge=0)
    detail: dict[str, Any] = Field(default_factory=dict)  # minimal, no raw payloads


class CacheEntry(Entity):
    cache_key: str
    company_id: UUID
    person_key: str
    domain: str
    email: str | None
    email_verification: EmailVerificationStatus
    verified_at: AwareDatetime | None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    provider: ProviderName | None
    title: str | None = None
    linkedin_url: str | None = None
    phone: str | None = None
    phone_status: PhoneStatus = PhoneStatus.NOT_REQUESTED
    provenance: dict[str, Any] = Field(default_factory=dict)
    cached_at: AwareDatetime = Field(default_factory=utcnow)
    expires_at: AwareDatetime

    def fresh(self, now: AwareDatetime) -> bool:
        return self.expires_at > now
