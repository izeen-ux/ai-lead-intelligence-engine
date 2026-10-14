"""Offline demo world for CLI dry runs (synthetic labeled cases; no DB, no network, no cost).

Companies are QUALIFIED (or not) by the REAL Phase 5 service with mock Laya and enriched by the
REAL Phase 6 service with mock providers - the gates are never bypassed, even in demos.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from app.core.config import ProviderMode, Settings
from app.domain.analysis.records import AnalysisStatus
from app.domain.enrichment.identity import PageText
from app.domain.enums import ContactStatus, DomainResolutionStatus, QualificationDecision
from app.evaluation.qualification import case_input, load_dataset
from app.providers.enrichment_kit.mock import MockEnrichmentKitProvider
from app.providers.hunter.mock import MockHunterProvider
from app.providers.laya import MockLayaProvider
from app.providers.registry import ProviderRegistry, mock_reasoning
from app.repositories.analysis import InMemoryAnalysisStore
from app.repositories.enrichment import CompanyContext, InMemoryEnrichmentStore
from app.repositories.outputs import InMemoryOutputStore
from app.repositories.pipeline import InMemoryPipelineStore
from app.repositories.qualification import InMemoryQualificationStore
from app.services.enrichment import EnrichmentService
from app.services.lead_analysis import LeadAnalysisService
from app.services.leads import LeadViewService
from app.services.pipeline import PipelineService
from app.services.pipeline_stages import core_stages
from app.services.qualification import QualificationService

# (labeled case id -> synthetic team/about page on the company's own domain)
# The three cases focused tests build on: two QUALIFIED leads and one REVIEW.
CORE_DEMO_PAGES = {
    "Q19": (
        "## About Quikcart\nQuikcart runs 10-minute grocery delivery from dark stores in Pune and "
        "Bengaluru and is adding new stores every month.\n### Riya Kapoor - Founder & CEO\n"
        "Riya Kapoor is the CEO of Quikcart."
    ),
    "Q11": (
        "## Ecoride Fleet\nElectric three-wheeler fleet for last-mile deliveries; operations team "
        "schedules drivers, charging and maintenance across cities.\n"
        "### Arjun Missra - Co-Founder & CEO"
    ),
    "Q25": "### Dev Rao - Founder & CEO",  # REVIEW in Phase 5 -> blocked everywhere downstream
}

# The manager demo: every company is SYNTHETIC (labelled evaluation cases). Six pass the hard
# ICP rules through the normal services (funding path, intent path or both), one is REVIEW and
# one is NOT_QUALIFIED so the gate is visible. Founder profile links on these pages use the
# ``synthetic-fixture-`` slug so nobody can mistake them for real profiles.
DEMO_PAGES = {
    **CORE_DEMO_PAGES,
    "Q01": (
        "## About Kheti Labs\nKheti Labs builds software that automates procurement between "
        "farmers and food processors across Maharashtra.\n### Neha Kulkarni - Founder & CEO\n"
        "[LinkedIn](https://www.linkedin.com/in/synthetic-fixture-neha-kulkarni)"
    ),
    "Q02": (
        "## Parcelwise\nParcelwise is a logistics platform that plans last-mile routes for "
        "regional courier companies.\n### Kabir Sethi - Co-Founder & CEO"
    ),
    "Q09": (
        "## Ledgerly\nLedgerly automates invoicing and collections for small manufacturers; "
        "finance teams reconcile payments in one place.\n### Ananya Iyer - Founder & CEO"
    ),
    "Q10": (
        "## Freightmind\nFreightmind automates dispatch and freight matching for fleet "
        "operators.\n### Rohan Nobody - Founder"  # mock: no provider finds this person
    ),
    "Q04": "### Tara Menon - Founder & CEO",  # round above the Rs 5 Cr cap -> gate says no
}

# Mock providers are free, so the demo raises only the per-run COUNT budgets (not a rule, not
# a gate) to let every qualified synthetic company get a contact in one run, and switches the
# optional phone capability on so the synthetic ``+91 00000 ...`` numbers are visible.
DEMO_BUDGETS: dict[str, object] = {
    "enrichment_max_hunter_credits_per_run": 30,
    "enrichment_max_apollo_credits_per_run": 10,
    "enrichment_max_sidecar_calls_per_run": 60,
    "enrichment_max_cost_usd_per_run": Decimal("2.00"),
    "enrichment_phone_enabled": True,
    "enrichment_max_phone_lookups_per_run": 30,
    "analysis_max_per_run": 10,
}


def demo_settings(settings: Settings) -> Settings:
    """Settings for SYNTHETIC data only (in-memory demo / the ``lead_intel_demo`` database)."""
    return settings.model_copy(update=DEMO_BUDGETS)


def demo_context(case_id: str, name: str, page: str) -> dict[str, object]:
    """Synthetic company facts a real crawl would have stored (description, link metadata)."""
    first = page.split("\n###", 1)[0].replace("#", "").strip()
    slug = "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-")
    return {
        "description": first if len(first) > 20 else None,
        "social_links": {
            "linkedin.com": f"https://www.linkedin.com/company/synthetic-fixture-{slug}"
        },
        "funding_round": None,
    }


@dataclass
class DemoWorld:
    qualifications: InMemoryQualificationStore
    enrichment: InMemoryEnrichmentStore
    company_ids: list[uuid.UUID]


async def build_demo_world(settings: Settings, cases: dict[str, str] | None = None) -> DemoWorld:
    pages = cases or DEMO_PAGES
    settings = demo_settings(settings)
    dataset = load_dataset()
    qstore, estore = InMemoryQualificationStore(), InMemoryEnrichmentStore()
    ids: list[uuid.UUID] = []
    for case in dataset.cases:
        if case.id not in pages:
            continue
        data = case_input(case, dataset.reference_date)
        qstore.add(data)
        estore.add_company(
            CompanyContext(
                company_id=data.facts.company_id,
                name=data.facts.name,
                domain=data.facts.domain,
                domain_status=DomainResolutionStatus(data.facts.domain_status),
                pages=(
                    PageText(
                        url=f"https://{data.facts.domain}/about",
                        markdown=pages[case.id],
                        page_id=uuid.uuid5(uuid.NAMESPACE_URL, f"{case.id}:about"),
                    ),
                ),
                **demo_context(case.id, data.facts.name, pages[case.id]),  # type: ignore[arg-type]
            )
        )
        ids.append(data.facts.company_id)
    await QualificationService(qstore, MockLayaProvider(settings), settings).run(ids)
    await EnrichmentService(
        estore, qstore, MockEnrichmentKitProvider(settings), MockHunterProvider(settings), settings
    ).run(ids)
    return DemoWorld(qstore, estore, ids)


@dataclass
class DemoOutputs:
    """Phase 8 demo: the demo world + mock-router analyses + in-memory output store."""

    world: DemoWorld
    analyses: InMemoryAnalysisStore
    outputs: InMemoryOutputStore
    views: LeadViewService


async def build_demo_outputs(
    settings: Settings, cases: dict[str, str] | None = None
) -> DemoOutputs:
    world = await build_demo_world(settings, cases)
    settings = demo_settings(settings)
    analyses = InMemoryAnalysisStore()
    await LeadAnalysisService(
        analyses, world.qualifications, world.enrichment, mock_reasoning(settings), settings
    ).run(world.company_ids, limit=len(world.company_ids))
    outputs = InMemoryOutputStore(world.qualifications, world.enrichment, analyses)
    views = LeadViewService(world.qualifications, world.enrichment, analyses)
    return DemoOutputs(world, analyses, outputs, views)


def demo_pipeline(settings: Settings, demo: DemoOutputs) -> PipelineService:
    """In-memory pipeline over the demo world (qualification -> export, mock providers).
    Writes go to memory only, so a non-dry run is allowed here without live mode."""
    settings = demo_settings(settings)  # synthetic data + mock providers only
    q, e, a = demo.world.qualifications, demo.world.enrichment, demo.analyses

    def latest_qualified() -> list[tuple[uuid.UUID, uuid.UUID]]:
        out = []
        for cid in demo.world.company_ids:
            d = q.latest_decision(cid)
            if d is not None and d.decision is QualificationDecision.QUALIFIED:
                out.append((cid, d.id))
        return out

    def has_contact(cid: uuid.UUID, decision_id: uuid.UUID) -> bool:
        return any(
            c.qualification_id == decision_id and c.status is ContactStatus.VERIFIED
            for c in e.contacts(cid)
        )

    def needs_decision(limit: int) -> list[uuid.UUID]:
        return [c for c in demo.world.company_ids if q.latest_decision(c) is None][:limit]

    def needs_contact(limit: int) -> list[uuid.UUID]:
        return [c for c, d in latest_qualified() if not has_contact(c, d)][:limit]

    def needs_analysis(limit: int) -> list[uuid.UUID]:
        return [
            c
            for c, d in latest_qualified()
            if has_contact(c, d)
            and not any(
                r.qualification_id == d and r.status is AnalysisStatus.SUCCEEDED
                for r in a.analyses(c)
            )
        ][:limit]

    runners = core_stages(
        settings,
        ProviderRegistry(settings.model_copy(update={"provider_mode": ProviderMode.MOCK})),
        qualifications=q,
        enrichment=e,
        analyses=a,
        outputs=demo.outputs,
        qualification_candidates=needs_decision,
        enrichment_candidates=needs_contact,
        analysis_candidates=needs_analysis,
    )
    return PipelineService(InMemoryPipelineStore(), runners, settings, allow_mock_writes=True)
