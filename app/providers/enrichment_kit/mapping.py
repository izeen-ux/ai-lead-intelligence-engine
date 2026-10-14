"""Sidecar payload -> domain models. Vendor-specific field names end here."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from app.core.errors import ProviderMalformedResponseError
from app.domain.enums import (
    EmailVerificationStatus,
    EnrichmentStatus,
    EnrichmentTarget,
    ProviderName,
    ProviderOperation,
)
from app.domain.models import EnrichmentResult, utcnow
from app.providers.contracts import DiscoveredPerson, EmailVerificationResult

SIDECAR_PROVIDERS = {"hunter": ProviderName.HUNTER, "apollo": ProviderName.APOLLO}
OPERATION = {
    ProviderName.HUNTER: ProviderOperation.EMAIL_FINDER,
    ProviderName.APOLLO: ProviderOperation.PERSON_MATCH,
}
VERIFICATION = {
    "valid": EmailVerificationStatus.VALID,
    "invalid": EmailVerificationStatus.INVALID,
    "accept_all": EmailVerificationStatus.ACCEPT_ALL,
    "webmail": EmailVerificationStatus.WEBMAIL,
    "disposable": EmailVerificationStatus.DISPOSABLE,
    "unknown": EmailVerificationStatus.UNKNOWN,
    "pending": EmailVerificationStatus.PENDING,
}


def _str(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _unit(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return min(max(float(value), 0.0), 1.0)


_PHONE_CHARS = re.compile(r"^\+?[\d\s().\-]{7,24}$")


def phone_of(value: Any) -> str | None:
    """A provider-returned phone number, kept only when it looks like one (7-15 digits)."""
    text = _str(value)
    if text is None or not _PHONE_CHARS.match(text):
        return None
    return text if 7 <= sum(ch.isdigit() for ch in text) <= 15 else None


def provider_of(name: Any, orchestrator: str) -> ProviderName:
    provider = SIDECAR_PROVIDERS.get(str(name))
    if provider is None:
        raise ProviderMalformedResponseError(orchestrator, f"unknown provider {name!r} in response")
    return provider


def attempt_from_output(
    entry: dict[str, Any],
    *,
    lead_id: UUID,
    target: EnrichmentTarget,
    orchestrator: str,
    include_phone: bool = False,
) -> EnrichmentResult:
    """One provider attempt of the waterfall (hit, miss or typed error). Emails stay
    UNVERIFIED here: a provider's own 'verified' claim is only kept as evidence."""
    if not isinstance(entry, dict):
        raise ProviderMalformedResponseError(orchestrator, "attempt is not an object")
    provider = provider_of(entry.get("provider"), orchestrator)
    base: dict[str, Any] = {
        "lead_id": lead_id,
        "target": target,
        "provider": provider,
        "orchestrated_by": ProviderName.ENRICHMENT_KIT,
        "latency_ms": int(entry.get("latencyMs") or 0),
    }
    now = utcnow()
    base.update(attempted_at=now, completed_at=now)  # one clock read: completed >= attempted
    op = OPERATION[provider].value
    kind = _str(entry.get("errorKind"))
    if kind is not None:
        return EnrichmentResult(
            **base,
            status=EnrichmentStatus.FAILED
            if entry.get("called", True)
            else EnrichmentStatus.SKIPPED,
            fields={"operation": op, "http_status": entry.get("statusCode")},
            error=f"{provider.value}: {kind}",
            error_code=kind,
            cost_credits=Decimal(0),
        )
    out = entry.get("output")
    if out is None:
        return EnrichmentResult(
            **base,
            status=EnrichmentStatus.NOT_FOUND,
            fields={"operation": op},
            cost_credits=Decimal(0),
        )
    if not isinstance(out, dict):
        raise ProviderMalformedResponseError(orchestrator, "provider output is not an object")
    confidence = _unit(out.get("confidence"))
    if confidence is None:
        raise ProviderMalformedResponseError(orchestrator, "provider output without confidence")
    email = _str(out.get("email"))
    evidence = out.get("evidence") if isinstance(out.get("evidence"), dict) else {}
    try:
        return EnrichmentResult(
            **base,
            status=EnrichmentStatus.SUCCESS if email else EnrichmentStatus.PARTIAL,
            email=email,
            # never kept unless the phone capability was requested for this call
            phone=phone_of(out.get("phone")) if include_phone else None,
            confidence=confidence,
            # Hunter charges a search credit only when an email is found; Apollo when matched.
            cost_credits=Decimal(1) if (email or provider is ProviderName.APOLLO) else Decimal(0),
            fields={
                "operation": op,
                "first_name": _str(out.get("firstName")),
                "last_name": _str(out.get("lastName")),
                "full_name": _str(out.get("fullName")),
                "title": _str(out.get("title")),
                "company_domain": _str(out.get("companyDomain")),
                "linkedin_url": _str(out.get("linkedinUrl")),
                "provider_says_verified": bool(out.get("emailVerified")),
                "evidence": evidence,
            },
        )
    except ValidationError:
        raise ProviderMalformedResponseError(
            orchestrator, f"{provider.value} output failed validation (email format?)"
        ) from None


def verification_from(body: dict[str, Any], provider: ProviderName) -> EmailVerificationResult:
    email = _str(body.get("email"))
    if email is None:
        raise ProviderMalformedResponseError(provider.value, "verification without email")
    kind = _str(body.get("errorKind"))
    raw_status = _str(body.get("status"))
    status = VERIFICATION.get(raw_status or "", EmailVerificationStatus.UNKNOWN)
    score = body.get("score")
    return EmailVerificationResult(
        provider=provider,
        email=email.lower(),
        status=EmailVerificationStatus.UNKNOWN if kind else status,
        score=None if score is None else _unit(float(score) / 100),
        error_code=kind,
        http_status=body.get("httpStatus") if isinstance(body.get("httpStatus"), int) else None,
    )


def person_from(raw: Any) -> DiscoveredPerson | None:
    if not isinstance(raw, dict):
        return None
    conf = raw.get("confidence")
    raw_verification = raw.get("verification")
    verification: dict[str, Any] = raw_verification if isinstance(raw_verification, dict) else {}
    return DiscoveredPerson(
        first_name=_str(raw.get("firstName")),
        last_name=_str(raw.get("lastName")),
        position=_str(raw.get("position")),
        seniority=_str(raw.get("seniority")),
        email=_str(raw.get("email")),
        confidence=None if conf is None else _unit(float(conf) / 100),
        verification_status=_str(verification.get("status")),
        verification_date=_str(verification.get("date")),
        sources_count=int(raw.get("sourcesCount") or 0),
    )


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=utcnow().tzinfo)
