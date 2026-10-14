"""Live enrichment-kit orchestrator (Phase 6) - the single enrichment entry point.

enrichment-kit (github.com/masteranime/enrichment-kit @ 6998898c03, MIT, TypeScript) is a
library + CLI without an HTTP mode and is not on npm, so it runs inside the project-owned Node
sidecar ``sidecars/enrichment-kit`` (127.0.0.1). The sidecar runs the upstream Waterfall
(Hunter email-finder, then Apollo people/match; early exit at the configured confidence) and
returns every provider attempt. Classified as PAID: it spends Hunter/Apollo credits.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx2

from app.core.config import Settings
from app.core.errors import ProviderMalformedResponseError, ProviderNotConfiguredError
from app.domain.enums import ProviderName
from app.providers.base import LiveAdapterBase
from app.providers.contracts import (
    EnrichmentOutcome,
    EnrichmentRequest,
    PeopleSearchRequest,
    PeopleSearchResult,
    ProviderStatus,
)
from app.providers.enrichment_kit.client import SidecarClient, endpoint_problem, error_for_kind
from app.providers.enrichment_kit.mapping import attempt_from_output, person_from


def split_name(full: str | None) -> tuple[str | None, str | None]:
    parts = (full or "").split()
    if not parts:
        return None, None
    return parts[0], " ".join(parts[1:]) or None


class EnrichmentKitAdapter(LiveAdapterBase):
    name = ProviderName.ENRICHMENT_KIT
    implemented_in_phase = 6
    operational = True

    def __init__(
        self, settings: Settings, *, transport: httpx2.AsyncBaseTransport | None = None
    ) -> None:
        super().__init__(settings)
        self._sidecar = SidecarClient(settings, self.name.value, transport=transport)

    def _problem(self) -> str | None:
        problem = endpoint_problem(self._settings)
        if problem is None and not (
            self._settings.provider_has_credentials(ProviderName.HUNTER)
            or self._settings.provider_has_credentials(ProviderName.APOLLO)
        ):
            problem = "neither HUNTER_API_KEY nor APOLLO_API_KEY is set"
        return problem

    def status(self) -> ProviderStatus:
        base = super().status()
        problem = self._problem()
        return base.model_copy(
            update={
                "configured": problem is None,
                "operational": problem is None,
                "detail": problem,
            }
        )

    def _ensure_callable(self, operation: str) -> None:
        super()._ensure_callable(operation)  # paid guard first
        problem = self._problem()
        if problem is not None:
            raise ProviderNotConfiguredError(
                self.name.value, f"{operation}: {problem}", details={"operation": operation}
            )

    async def health(self) -> dict[str, Any]:
        """Sidecar /health: configuration + counters only. Never spends credits and is allowed
        without the paid switch (it is local)."""
        problem = endpoint_problem(self._settings)
        if problem is not None:
            raise ProviderNotConfiguredError(self.name.value, problem)
        return await self._sidecar.get("/health")

    async def account_usage(self) -> dict[str, Any]:
        """Hunter account usage counters via the sidecar (Hunter's account endpoint is free;
        personal account data is dropped by the sidecar). Used to RECORD real live-test usage."""
        problem = endpoint_problem(self._settings)
        if problem is not None:
            raise ProviderNotConfiguredError(self.name.value, problem)
        return await self._sidecar.post("/v1/account", {})

    async def enrich(self, request: EnrichmentRequest) -> EnrichmentOutcome:
        self._ensure_callable("enrich")
        first, last = request.first_name, request.last_name
        if not (first or last):
            first, last = split_name(request.person_name)
        providers = [
            p.value for p in request.providers if p in (ProviderName.HUNTER, ProviderName.APOLLO)
        ]
        body = await self._sidecar.post(
            "/v1/enrich",
            {
                "input": {
                    "fullName": request.person_name,
                    "firstName": first,
                    "lastName": last,
                    "company": request.company_name,
                    "domain": request.domain,
                    "linkedinUrl": str(request.linkedin_url) if request.linkedin_url else None,
                },
                "providers": providers,
                "minConfidence": request.min_confidence,
                "earlyExitOnConfidence": request.early_exit_confidence,
                "includePhone": request.include_phone,
            },
        )
        outputs = body.get("outputs")
        if not isinstance(outputs, list):
            raise ProviderMalformedResponseError(self.name.value, "response has no 'outputs'")
        attempts = [
            attempt_from_output(
                o,
                lead_id=request.lead_id,
                target=request.target,
                orchestrator=self.name.value,
                include_phone=request.include_phone,
            )
            for o in outputs
        ]
        best = body.get("best")
        final = None
        if isinstance(best, dict) and best.get("email"):
            final = next(
                (a for a in attempts if a.email and a.email.lower() == str(best["email"]).lower()),
                None,
            )
        cost = body.get("totalCost")
        return EnrichmentOutcome(
            orchestrator=self.name,
            final=final,
            attempts=attempts,
            total_cost_usd=Decimal(str(cost)) if isinstance(cost, int | float) else None,
        )

    async def find_people(self, request: PeopleSearchRequest) -> PeopleSearchResult:
        self._ensure_callable("find_people")
        body = await self._sidecar.post("/v1/people", request.model_dump())
        kind = body.get("errorKind")
        if kind in ("auth", "quota_exhausted", "forbidden"):
            raise error_for_kind(ProviderName.HUNTER.value, str(kind), "people search")
        raw = body.get("people")
        if not isinstance(raw, list):
            raise ProviderMalformedResponseError(self.name.value, "response has no 'people'")
        people = [p for p in (person_from(r) for r in raw) if p is not None]
        return PeopleSearchResult(
            provider=ProviderName.HUNTER,
            people=people,
            error_code=str(kind) if kind else None,
            credits_used=1 if people else 0,  # Domain Search: 1 credit when results
        )
