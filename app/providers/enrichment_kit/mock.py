"""Deterministic offline enrichment waterfall (PROVIDER_MODE=mock, tests, dry runs).

Behaviour is chosen by a keyword in the person's name (case-insensitive), so every Phase 6
path can be exercised offline:

  (none)       Hunter hit, confidence 0.92          -> early exit, Apollo not called
  miss         Hunter miss                          -> Apollo hit 0.90
  nobody       Hunter miss, Apollo miss             -> no result
  lowconf      Hunter 0.40 (below min), Apollo miss -> no acceptable result
  invalid      Hunter hit, address fails verification (see MockHunterProvider)
  acceptall    Hunter hit, catch-all domain         -> stays unverified
  timeout      Hunter timeout (typed)               -> Apollo hit
  ratelimit    Hunter rate limited (typed)          -> Apollo hit
  quota        Hunter usage limit (typed)           -> Apollo hit
  mismatch     Hunter returns an address on ANOTHER domain
  wrongperson  Hunter returns a DIFFERENT person
  malformed    sidecar returns an unusable payload  -> ProviderMalformedResponseError
Every hit carries a ``synthetic-fixture-`` LinkedIn URL; a ``+91 00000 ...`` phone is returned
only when the request asks for one (``include_phone``). No network, no cost. Requests are
recorded in ``requests`` for assertions.
"""

from __future__ import annotations

import re
from decimal import Decimal

from app.core.errors import ProviderMalformedResponseError
from app.domain.enums import EnrichmentStatus, EnrichmentTarget, ProviderName
from app.domain.models import EnrichmentResult
from app.providers.base import MockProviderBase
from app.providers.contracts import (
    DiscoveredPerson,
    EnrichmentOutcome,
    EnrichmentRequest,
    PeopleSearchRequest,
    PeopleSearchResult,
)

_KEYWORDS = (
    "wrongperson",
    "acceptall",
    "malformed",
    "mismatch",
    "ratelimit",
    "lowconf",
    "timeout",
    "invalid",
    "nobody",
    "quota",
    "miss",
)


def scenario(request: EnrichmentRequest) -> str:
    name = " ".join(
        x for x in (request.person_name, request.first_name, request.last_name) if x
    ).lower()
    return next((k for k in _KEYWORDS if k in name), "default")


def _names(request: EnrichmentRequest) -> tuple[str, str]:
    parts = (request.person_name or "").split()
    first = request.first_name or (parts[0] if parts else "founder")
    last = request.last_name or (" ".join(parts[1:]) if len(parts) > 1 else "")
    return first, last


def _local(first: str, last: str) -> str:
    return ".".join(re.sub(r"[^a-z0-9]", "", p.lower()) for p in (first, last) if p) or "founder"


def synthetic_linkedin(local: str) -> str:
    """Obviously synthetic profile URL (the ``synthetic-fixture-`` slug is never a real one)."""
    return f"https://www.linkedin.com/in/synthetic-fixture-{local.replace('.', '-')}"


def synthetic_phone(local: str) -> str:
    """Obviously synthetic number: +91 00000 is not an assignable Indian mobile prefix."""
    return f"+91 00000 {sum(local.encode()) % 100000:05d}"


class MockEnrichmentKitProvider(MockProviderBase):
    name = ProviderName.ENRICHMENT_KIT

    def __init__(self, settings=None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(settings)
        self.requests: list[EnrichmentRequest] = []
        self.people_requests: list[PeopleSearchRequest] = []

    def _result(
        self, request: EnrichmentRequest, provider: ProviderName, **kw: object
    ) -> EnrichmentResult:
        return EnrichmentResult(
            lead_id=request.lead_id,
            target=request.target,
            provider=provider,
            orchestrated_by=self.name,
            latency_ms=5,
            **kw,  # type: ignore[arg-type]
        )

    def _hit(
        self,
        request: EnrichmentRequest,
        provider: ProviderName,
        email: str,
        confidence: float,
        **fields: object,
    ) -> EnrichmentResult:
        first, last = _names(request)
        local = _local(first, last)
        base_fields: dict[str, object] = {
            "operation": "email_finder" if provider is ProviderName.HUNTER else "person_match",
            "first_name": first,
            "last_name": last or None,
            "full_name": f"{first} {last}".strip(),
            "title": request.title,
            "company_domain": request.domain,
            # Hunter's email finder also returns a public profile URL when it knows one.
            "linkedin_url": synthetic_linkedin(local),
            "provider_says_verified": provider is ProviderName.APOLLO,
            "evidence": {},
        }
        base_fields.update(fields)
        return self._result(
            request,
            provider,
            status=EnrichmentStatus.SUCCESS,
            email=email,
            # only Hunter's finder response can carry a number, and only when asked
            phone=synthetic_phone(local)
            if request.include_phone and provider is ProviderName.HUNTER
            else None,
            confidence=confidence,
            cost_credits=Decimal(1),
            fields=base_fields,
        )

    def _miss(self, request: EnrichmentRequest, provider: ProviderName) -> EnrichmentResult:
        op = "email_finder" if provider is ProviderName.HUNTER else "person_match"
        return self._result(
            request,
            provider,
            status=EnrichmentStatus.NOT_FOUND,
            cost_credits=Decimal(0),
            fields={"operation": op},
        )

    def _error(
        self, request: EnrichmentRequest, provider: ProviderName, kind: str
    ) -> EnrichmentResult:
        return self._result(
            request,
            provider,
            status=EnrichmentStatus.FAILED,
            error=f"{provider.value}: {kind}",
            error_code=kind,
            cost_credits=Decimal(0),
            fields={"operation": "email_finder"},
        )

    async def enrich(self, request: EnrichmentRequest) -> EnrichmentOutcome:
        self.calls.append("enrich")
        self.requests.append(request)
        if request.target is EnrichmentTarget.PERSON and not request.domain:
            miss = self._miss(request, ProviderName.HUNTER)
            return EnrichmentOutcome(orchestrator=self.name, final=None, attempts=[miss])
        kind = scenario(request)
        if kind == "malformed":
            raise ProviderMalformedResponseError(self.name.value, "mock malformed payload")
        domain = request.domain or "unknown.example"
        first, last = _names(request)
        local = _local(first, last)
        allowed = request.providers
        attempts: list[EnrichmentResult] = []
        hunter: EnrichmentResult | None = None
        if ProviderName.HUNTER in allowed:
            if kind in ("miss", "nobody"):
                hunter = self._miss(request, ProviderName.HUNTER)
            elif kind in ("timeout", "ratelimit", "quota"):
                code = {"ratelimit": "rate_limited", "quota": "quota_exhausted"}.get(kind, kind)
                hunter = self._error(request, ProviderName.HUNTER, code)
            elif kind == "lowconf":
                hunter = self._hit(request, ProviderName.HUNTER, f"{local}@{domain}", 0.4)
            elif kind == "mismatch":
                hunter = self._hit(
                    request,
                    ProviderName.HUNTER,
                    f"{local}@otherco.example",
                    0.92,
                    company_domain="otherco.example",
                )
            elif kind == "wrongperson":
                hunter = self._hit(
                    request,
                    ProviderName.HUNTER,
                    f"someone.else@{domain}",
                    0.92,
                    first_name="Someone",
                    last_name="Else",
                    full_name="Someone Else",
                )
            else:
                prefix = {"invalid": "invalid.", "acceptall": "acceptall."}.get(kind, "")
                hunter = self._hit(request, ProviderName.HUNTER, f"{prefix}{local}@{domain}", 0.92)
            attempts.append(hunter)
        hunter_ok = (
            hunter is not None
            and hunter.status is EnrichmentStatus.SUCCESS
            and (hunter.confidence or 0) >= request.early_exit_confidence
        )
        final = hunter if hunter_ok else None
        if not hunter_ok and ProviderName.APOLLO in allowed:
            if kind in ("nobody", "lowconf"):
                attempts.append(self._miss(request, ProviderName.APOLLO))
            else:
                apollo = self._hit(
                    request,
                    ProviderName.APOLLO,
                    f"{local}@{domain}",
                    0.9,
                    evidence={"email_status": "verified", "match_confidence": "high"},
                )
                attempts.append(apollo)
                final = apollo
        return EnrichmentOutcome(
            orchestrator=self.name,
            final=final,
            attempts=attempts,
            total_cost_usd=Decimal("0"),
        )

    async def find_people(self, request: PeopleSearchRequest) -> PeopleSearchResult:
        self.calls.append("find_people")
        self.people_requests.append(request)
        if request.domain.startswith("nopeople"):
            return PeopleSearchResult(provider=ProviderName.HUNTER, people=[])
        people = [
            DiscoveredPerson(
                first_name="Asha",
                last_name="Rao",
                position="Founder & CEO",
                seniority="executive",
                email=f"asha@{request.domain}",
                confidence=0.9,
            ),
            DiscoveredPerson(
                first_name="Vikram",
                last_name="Nair",
                position="Head of Sales",
                seniority="executive",
                email=f"vikram@{request.domain}",
                confidence=0.8,
            ),
        ]
        return PeopleSearchResult(
            provider=ProviderName.HUNTER, people=people[: request.limit], credits_used=1
        )
