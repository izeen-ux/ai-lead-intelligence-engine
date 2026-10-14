"""Phase 6 unit-test fixtures: companies QUALIFIED (or not) by the REAL Phase 5 service."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from app.core.config import Settings
from app.domain.enrichment.identity import PageText
from app.domain.enums import DomainResolutionStatus, SignalType
from app.providers.enrichment_kit.mock import MockEnrichmentKitProvider
from app.providers.hunter.mock import MockHunterProvider
from app.providers.laya import MockLayaProvider
from app.repositories.enrichment import CompanyContext, InMemoryEnrichmentStore
from app.repositories.qualification import InMemoryQualificationStore, QualificationInput
from app.services.enrichment import EnrichmentService
from app.services.qualification import QualificationService
from tests.qualification_factories import NOW, facts, sig

LATER = NOW + timedelta(hours=1)


def team(*lines: str) -> str:
    return "## Team\n" + "\n".join(f"### {line}" for line in lines)


@dataclass
class World:
    settings: Settings
    estore: InMemoryEnrichmentStore
    qstore: InMemoryQualificationStore
    kit: MockEnrichmentKitProvider
    hunter: MockHunterProvider
    ids: list[uuid.UUID]

    def service(self, *, clock=lambda: LATER, **kw: Any) -> EnrichmentService:
        return EnrichmentService(
            self.estore, self.qstore, self.kit, self.hunter, kw.pop("settings", self.settings),
            clock=clock, **kw,
        )  # fmt: skip


def company(
    name: str = "Acme Robotics",
    *,
    people: str | None = None,
    qualify: str = "qualified",  # qualified | review | unverified
    domain: str = "acmerobotics.in",
) -> tuple[QualificationInput, CompanyContext]:
    cid = uuid.uuid4()
    status = (
        DomainResolutionStatus.REVIEW
        if qualify == "unverified"
        else DomainResolutionStatus.VERIFIED
    )
    signals = (
        [sig(f"{name} raises seed funding from angels", source=f"{cid}")]
        if qualify == "review"
        else [sig(f"{name} raises Rs 4 crore seed round", source=f"{cid}")]
    )
    f = facts(company_id=cid, name=name, domain=None if qualify == "unverified" else domain,
              domain_status=status)  # fmt: skip
    pages = (PageText(url=f"https://{domain}/team", markdown=people or "", page_id=uuid.uuid4()),)
    return QualificationInput(f, signals), CompanyContext(cid, name, f.domain, status, pages)


async def world(make_settings, *companies: tuple[QualificationInput, CompanyContext],
                **settings_kw: Any) -> World:  # fmt: skip
    settings = make_settings(**settings_kw)
    qstore, estore = InMemoryQualificationStore(), InMemoryEnrichmentStore()
    ids = []
    for qin, ctx in companies:
        qstore.add(qin)
        estore.add_company(ctx)
        ids.append(ctx.company_id)
    await QualificationService(qstore, MockLayaProvider(), settings, clock=lambda: NOW).run(ids)
    for cid in ids:
        latest = qstore.latest_decision(cid)
        if latest is not None and latest.enrichment_eligible:
            estore.qualified.append(cid)
    return World(settings, estore, qstore, MockEnrichmentKitProvider(), MockHunterProvider(), ids)


__all__ = ["LATER", "NOW", "SignalType", "World", "company", "team", "world"]
