from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from pydantic import HttpUrl

from app.domain.enums import ProviderName, SourceType
from app.domain.models import utcnow
from app.providers.base import MockProviderBase, RunStartedHook
from app.providers.contracts import CandidateRecord, DiscoveryQuery, DiscoveryResult

_FIXTURES = (
    ("Acme Robotics", "acme.example", "Asha Rao", "Founder & CEO"),
    ("Globex Analytics", "globex.example", "Vikram Nair", "Co-founder"),
    ("Initech Logistics", "initech.example", "Meera Shah", "CEO"),
)


class MockApifyProvider(MockProviderBase):
    name = ProviderName.APIFY

    async def discover(
        self, query: DiscoveryQuery, *, on_run_started: RunStartedHook | None = None
    ) -> DiscoveryResult:
        self.calls.append("discover")
        # unique per call: provider run ids are unique in ``discovery_runs`` and a pipeline
        # run starts one discovery run per enabled source
        run_ref = f"mock-run-{uuid4().hex[:12]}"
        if on_run_started is not None:
            await on_run_started(run_ref)
        started = utcnow()
        candidates = [
            CandidateRecord(
                company_name=company,
                domain=domain,
                founder_name=founder,
                founder_role=role,
                source_url=HttpUrl(f"https://news.example/funding/{domain.split('.')[0]}"),
                source_type=SourceType.NEWS,
                source_title=f"{company} raises seed funding",
                source_platform="news.example",
                raw={"query_source": query.source},
            )
            for company, domain, founder, role in _FIXTURES[: query.limit]
        ]
        return DiscoveryResult(
            provider=self.name,
            actor_id="mock/discovery",
            run_ref=run_ref,
            provider_status="SUCCEEDED",
            provider_input={"keywords": query.keywords},
            max_items=query.max_items or 1,
            max_charge_usd=query.max_charge_usd,
            started_at=started,
            finished_at=utcnow(),
            raw_item_count=1,
            candidates=candidates,
            estimated_cost_usd=Decimal(0),
            cost_usd=Decimal(0),
        )
