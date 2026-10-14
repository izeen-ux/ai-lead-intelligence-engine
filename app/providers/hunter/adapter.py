"""Live Hunter adapter (Phase 6) - routed through the enrichment sidecar.

Hunter's key lives only in the sidecar (it reads ``HUNTER_API_KEY`` from .env); this adapter
checks it is present, runs the paid guard and calls the sidecar:

* ``enrich``       -> ``/v1/enrich`` with providers=["hunter"] (upstream enrichment-kit
                      HunterProvider = Email Finder; 1 credit only when an email is found)
* ``verify_email`` -> ``/v1/verify`` (Email Verifier, 1 credit; statuses valid / invalid /
                      accept_all / webmail / disposable / unknown; HTTP 202 -> pending)

A found address stays UNVERIFIED until ``verify_email`` returns VALID. Account-level failures
(auth, usage limit) raise typed errors; per-address outcomes (timeout, 451 PII opt-out, rate
limit) come back as UNKNOWN with ``error_code`` so the pipeline can record and move on.
"""

from __future__ import annotations

import httpx2

from app.core.config import Settings
from app.core.errors import ProviderMalformedResponseError, ProviderNotConfiguredError
from app.domain.enums import EnrichmentStatus, EnrichmentTarget, ProviderName
from app.domain.models import EnrichmentResult
from app.providers.base import LiveAdapterBase
from app.providers.contracts import EmailVerificationResult, EnrichmentRequest, ProviderStatus
from app.providers.enrichment_kit.client import SidecarClient, endpoint_problem, error_for_kind
from app.providers.enrichment_kit.mapping import attempt_from_output, verification_from


class SidecarVendorAdapter(LiveAdapterBase):
    """Shared plumbing for a single vendor reached through the sidecar."""

    implemented_in_phase = 6
    operational = True

    def __init__(
        self, settings: Settings, *, transport: httpx2.AsyncBaseTransport | None = None
    ) -> None:
        super().__init__(settings)
        self._sidecar = SidecarClient(settings, self.name.value, transport=transport)

    def status(self) -> ProviderStatus:
        base = super().status()
        problem = endpoint_problem(self._settings) if base.configured else None
        if not base.configured:
            problem = f"{self.name.value.upper()}_API_KEY not set"
        return base.model_copy(
            update={
                "configured": problem is None,
                "operational": problem is None,
                "detail": problem,
            }
        )

    def _ensure_callable(self, operation: str) -> None:
        super()._ensure_callable(operation)  # paid guard -> credentials
        problem = endpoint_problem(self._settings)
        if problem is not None:
            raise ProviderNotConfiguredError(
                self.name.value, f"{operation}: {problem}", details={"operation": operation}
            )

    async def enrich(self, request: EnrichmentRequest) -> EnrichmentResult:
        self._ensure_callable("enrich")
        first, last = request.first_name, request.last_name
        body = await self._sidecar.post(
            "/v1/enrich",
            {
                "input": {
                    "fullName": request.person_name,
                    "firstName": first,
                    "lastName": last,
                    "company": request.company_name,
                    "domain": request.domain,
                },
                "providers": [self.name.value],
                "minConfidence": request.min_confidence,
                "earlyExitOnConfidence": request.early_exit_confidence,
            },
        )
        outputs = body.get("outputs")
        if not isinstance(outputs, list):
            raise ProviderMalformedResponseError(self.name.value, "response has no 'outputs'")
        if not outputs:
            return EnrichmentResult(
                lead_id=request.lead_id,
                target=request.target,
                provider=self.name,
                status=EnrichmentStatus.NOT_FOUND,
            )
        return attempt_from_output(
            outputs[0],
            lead_id=request.lead_id,
            target=request.target or EnrichmentTarget.PERSON,
            orchestrator=self.name.value,
        )


class HunterAdapter(SidecarVendorAdapter):
    name = ProviderName.HUNTER

    async def verify_email(self, email: str) -> EmailVerificationResult:
        self._ensure_callable("verify_email")
        body = await self._sidecar.post("/v1/verify", {"email": email})
        kind = body.get("errorKind")
        if kind in ("auth", "quota_exhausted", "forbidden"):
            raise error_for_kind(self.name.value, str(kind), "verification")
        return verification_from(body, self.name)
