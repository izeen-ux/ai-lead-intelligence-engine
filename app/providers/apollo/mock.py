from __future__ import annotations

from app.domain.enums import EnrichmentStatus, EnrichmentTarget, ProviderName
from app.domain.models import EnrichmentResult
from app.providers.base import MockProviderBase
from app.providers.contracts import EnrichmentRequest


class MockApolloProvider(MockProviderBase):
    name = ProviderName.APOLLO

    async def enrich(self, request: EnrichmentRequest) -> EnrichmentResult:
        self.calls.append("enrich")
        fields: dict[str, object] = (
            {"title": "Founder & CEO", "location": "Bengaluru, IN"}
            if request.target is EnrichmentTarget.PERSON
            else {"employee_count": 42, "industry": "software"}
        )
        return EnrichmentResult(
            lead_id=request.lead_id,
            target=request.target,
            provider=self.name,
            status=EnrichmentStatus.SUCCESS,
            fields=fields,
            confidence=0.8,
        )
