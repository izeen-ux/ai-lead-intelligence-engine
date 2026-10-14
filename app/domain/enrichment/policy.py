"""Acceptance, merge and verification rules for provider results (Phase 6).

* A provider result is ACCEPTED only if: confidence >= min, the email is on the verified domain
  (or a subdomain), the provider's company domain (if given) is the verified domain, and the
  returned person is not positively a different person. Otherwise it is rejected with a reason.
* Merge: the email with the highest confidence wins; on a tie Hunter wins (primary provider);
  Apollo NEVER overwrites a higher-confidence Hunter email. Missing fields (title, LinkedIn
  URL) may be complemented from any accepted result - each field records its provider.
* Verification: every final email needs a Hunter verification that is FRESH
  (<= VERIFICATION_TTL). A Hunter Email-Finder result that already carries Hunter's own
  verification (status valid, dated inside the TTL) counts; Apollo's "verified" claim never does.
* Final status: VALID + confidence >= accept -> VERIFIED; INVALID/DISPOSABLE -> INVALID_EMAIL;
  anything else with an email -> UNVERIFIED; no email -> NOT_FOUND.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.domain.enrichment.config import EnrichmentConfig
from app.domain.enrichment.identity import PersonIdentity, email_matches_domain, name_matches
from app.domain.enums import (
    ContactStatus,
    EmailVerificationStatus,
    EnrichmentStatus,
    ProviderName,
)
from app.domain.models import EnrichmentResult


def _domain_ok(company_domain: Any, domain: str) -> bool:
    if not company_domain:
        return True
    cd = str(company_domain).lower().removeprefix("www.")
    return cd == domain or cd.endswith("." + domain) or domain.endswith("." + cd)


@dataclass
class Assessment:
    accepted: list[EnrichmentResult] = field(default_factory=list)
    complements: list[EnrichmentResult] = field(default_factory=list)  # name/title, no email
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (provider, reason)


def assess(
    identity: PersonIdentity, attempts: list[EnrichmentResult], config: EnrichmentConfig
) -> Assessment:
    out = Assessment()
    for a in attempts:
        if a.status not in (EnrichmentStatus.SUCCESS, EnrichmentStatus.PARTIAL):
            continue
        f = a.fields
        if not name_matches(identity, f.get("first_name"), f.get("last_name"), f.get("full_name")):
            out.rejected.append((a.provider.value, "person_mismatch"))
            continue
        if not _domain_ok(f.get("company_domain"), identity.domain):
            out.rejected.append((a.provider.value, "company_domain_mismatch"))
            continue
        if a.email is None:
            out.complements.append(a)
            continue
        if not email_matches_domain(a.email, identity.domain):
            out.rejected.append((a.provider.value, "email_domain_mismatch"))
            continue
        if (a.confidence or 0.0) < config.min_confidence:
            out.rejected.append((a.provider.value, "below_min_confidence"))
            continue
        out.accepted.append(a)
    return out


@dataclass
class Draft:
    email: str | None
    provider: ProviderName | None
    confidence: float | None
    title: str | None
    linkedin_url: str | None
    provenance: dict[str, Any]
    best: EnrichmentResult | None
    phone: str | None = None
    phone_provider: ProviderName | None = None


def merge(identity: PersonIdentity, assessment: Assessment) -> Draft:
    ranked = sorted(
        assessment.accepted,
        key=lambda a: (-(a.confidence or 0.0), a.provider is not ProviderName.HUNTER),
    )
    best = ranked[0] if ranked else None
    provenance: dict[str, Any] = {
        "identity": {
            "sources": list(identity.sources),
            "source_ids": [str(i) for i in identity.source_ids],
        },
        "rejected": [{"provider": p, "reason": r} for p, r in assessment.rejected],
    }
    title, title_src = identity.title, "website"
    linkedin, linkedin_src = None, None
    for a in [*ranked, *assessment.complements]:
        if not title and a.fields.get("title"):
            title, title_src = a.fields["title"], a.provider.value
        if not linkedin and a.fields.get("linkedin_url"):
            linkedin, linkedin_src = a.fields["linkedin_url"], a.provider.value
    if best is not None:
        provenance["email"] = {
            "provider": best.provider.value,
            "confidence": best.confidence,
            "operation": best.fields.get("operation"),
            "corroborated_by": sorted(
                {a.provider.value for a in ranked[1:] if a.email == best.email}
            ),
            "provider_says_verified": bool(best.fields.get("provider_says_verified")),
        }
    provenance["title"] = title_src if title else None
    provenance["linkedin_url"] = linkedin_src
    # Phone: only from a result that passed the identity checks (never a complement).
    phone_from = next((a for a in ranked if a.phone), None)
    return Draft(
        email=best.email if best else None,
        provider=best.provider if best else None,
        confidence=best.confidence if best else None,
        title=title,
        linkedin_url=linkedin,
        provenance=provenance,
        best=best,
        phone=phone_from.phone if phone_from else None,
        phone_provider=phone_from.provider if phone_from else None,
    )


def _parse(date: Any) -> datetime | None:
    if not isinstance(date, str) or not date:
        return None
    try:
        parsed = datetime.fromisoformat(date.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)


def preverified(
    best: EnrichmentResult | None, now: datetime, config: EnrichmentConfig
) -> datetime | None:
    """Hunter's own fresh 'valid' verification carried by its Email-Finder result, if any."""
    if best is None or best.provider is not ProviderName.HUNTER:
        return None
    verification = (best.fields.get("evidence") or {}).get("verification") or {}
    if verification.get("status") != "valid":
        return None
    when = _parse(verification.get("date"))
    if when is None or now - when > timedelta(days=config.verification_ttl_days):
        return None
    return when


def verification_fresh(
    verified_at: datetime | None, now: datetime, config: EnrichmentConfig
) -> bool:
    return verified_at is not None and now - verified_at <= timedelta(
        days=config.verification_ttl_days
    )


def final_status(
    email: str | None,
    verification: EmailVerificationStatus,
    confidence: float | None,
    config: EnrichmentConfig,
) -> ContactStatus:
    if not email:
        return ContactStatus.NOT_FOUND
    if verification in (EmailVerificationStatus.INVALID, EmailVerificationStatus.DISPOSABLE):
        return ContactStatus.INVALID_EMAIL
    if (
        verification is EmailVerificationStatus.VALID
        and (confidence or 0) >= config.accept_confidence
    ):
        return ContactStatus.VERIFIED
    return ContactStatus.UNVERIFIED
