"""Phase 1 smoke check.

Boots the real application in-process with outbound network BLOCKED, then:
  1. loads configuration without secrets,
  2. calls /health and /health/ready through the full ASGI stack,
  3. drives every mock provider once in pipeline order,
  4. confirms every live adapter refuses to call out,
  5. asserts zero external network attempts.

Usage:  uv run python scripts/smoke_check.py
Exit code 0 = pass, 1 = fail.
"""

from __future__ import annotations

import asyncio
import contextlib
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import ProviderMode, Settings
from app.core.errors import ProviderError
from app.demo import build_demo_outputs, demo_pipeline
from app.domain.enums import EnrichmentTarget, ProviderName
from app.domain.outputs.records import ExportMode
from app.domain.pipeline import Trigger
from app.domain.qualification.bundle import LAYA_QUESTIONS
from app.main import create_app
from app.providers.contracts import (
    DiscoveryQuery,
    EnrichmentRequest,
    LlmReviewRequest,
    QualificationRequest,
    SignalResearchRequest,
    WebsiteExtractionRequest,
)
from app.providers.google_sheets import InMemorySheetsWriter
from app.providers.registry import ProviderRegistry
from app.services.dashboard import DashboardBackend
from app.services.sheets_export import SheetsExportService
from app.utils.network_guard import block_external_network

failures: list[str] = []


def check(ok: bool, label: str) -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        failures.append(label)


async def drive_mock_pipeline(registry: ProviderRegistry) -> set[ProviderName]:
    used: set[ProviderName] = set()
    found = await registry.discovery.discover(DiscoveryQuery(source="funding_news", limit=1))
    used.add(found.provider)
    cand = found.candidates[0]
    if cand.domain is None:
        raise RuntimeError("mock discovery returned a candidate without a domain")
    site = await registry.website.extract_site(WebsiteExtractionRequest(domain=cand.domain))
    used.add(site.provider)
    fallback = await registry.website_fallback.extract_site(
        WebsiteExtractionRequest(domain="js-heavy.example")
    )
    used.add(fallback.provider)
    research = await registry.signal_research.research(
        SignalResearchRequest(company_name=cand.company_name, founder_name=cand.founder_name)
    )
    used.add(research.provider)
    lead_id = uuid4()
    decision = await registry.qualification.evaluate(
        QualificationRequest(
            subject_id=lead_id, state={"domain": cand.domain}, questions=list(LAYA_QUESTIONS)
        )
    )
    used.add(decision.provider)
    review = await registry.llm_review.review(
        LlmReviewRequest(subject_id=lead_id, bundle={}, qualified_allowed=False)
    )
    used.add(review.provider)
    req = EnrichmentRequest(lead_id=lead_id, target=EnrichmentTarget.PERSON, domain=cand.domain)
    outcome = await registry.enrichment.enrich(req)
    used.add(outcome.orchestrator)
    used.add((await registry.email_vendor.enrich(req)).provider)
    used.add((await registry.people_vendor.enrich(req)).provider)
    return used


async def live_adapters_refuse(registry: ProviderRegistry) -> int:
    refused = 0
    req = EnrichmentRequest(lead_id=uuid4(), target=EnrichmentTarget.PERSON, domain="acme.io")
    calls = [
        registry.discovery.discover(DiscoveryQuery(source="x")),
        registry.website.extract_site(WebsiteExtractionRequest(domain="acme.io")),
        registry.website_fallback.extract_site(WebsiteExtractionRequest(domain="acme.io")),
        registry.signal_research.research(SignalResearchRequest(company_name="Acme")),
        registry.qualification.evaluate(
            QualificationRequest(subject_id=uuid4(), state={}, questions=list(LAYA_QUESTIONS))
        ),
        registry.llm_review.review(
            LlmReviewRequest(subject_id=uuid4(), bundle={}, qualified_allowed=False)
        ),
        registry.enrichment.enrich(req),
        registry.email_vendor.enrich(req),
        registry.people_vendor.enrich(req),
    ]
    for coro in calls:
        with contextlib.suppress(ProviderError):
            await coro
            continue
        refused += 1
    return refused


async def demo_exports(settings: Settings) -> tuple[DashboardBackend, list[tuple[int, int]]]:
    """Phase 8: demo leads -> in-memory workbook twice (second run must write nothing)."""
    demo = await build_demo_outputs(settings)
    sheet = InMemorySheetsWriter()
    results = []
    for _ in range(2):
        report = await SheetsExportService(
            demo.outputs, demo.views, settings, sheet, mode=ExportMode.MOCK
        ).run()
        results.append((report.run.rows_written, report.run.unchanged_count))
    backend = DashboardBackend(demo.outputs, demo.views, demo.world.qualifications, "demo")
    return backend, results


def main() -> int:
    with block_external_network() as attempts:
        print("1. configuration (from environment/.env; no secrets required)")
        settings = Settings()
        check(True, f"settings loaded: env={settings.app_env.value}")
        check(settings.provider_mode.value == "mock", "PROVIDER_MODE=mock")
        check(not settings.allow_paid_provider_calls, "ALLOW_PAID_PROVIDER_CALLS=false")

        print("2. application startup + health endpoints")
        with TestClient(create_app(settings)) as client:
            live = client.get("/health")
            check(live.status_code == 200, f"GET /health -> {live.status_code}")
            ready = client.get("/health/ready")
            body = ready.json()
            check(ready.status_code == 200, f"GET /health/ready -> {ready.status_code}")
            check(body["status"] in ("ok", "degraded"), f"readiness status={body['status']}")
            check(len(body["providers"]) == 9, f"{len(body['providers'])} providers reported")

        print("2b. dashboard + Sheets projection (Phase 8, demo data, in-memory workbook)")
        backend, runs = asyncio.run(demo_exports(settings))
        check(
            runs[0][0] > 0 and runs[1] == (0, runs[0][0]),
            f"export idempotent: first run wrote {runs[0][0]} rows, second {runs[1][0]}",
        )
        # this block checks the dashboard itself; operator auth is checked in 2c
        open_settings = settings.model_copy(update={"operator_api_token": None})
        with TestClient(create_app(open_settings, dashboard_backend=backend)) as client:
            page = client.get("/dashboard")
            check(page.status_code == 200, f"GET /dashboard -> {page.status_code}")
            overview = client.get("/api/v1/dashboard/overview")
            check(
                overview.status_code == 200,
                f"GET /api/v1/dashboard/overview -> {overview.status_code}",
            )
            check(len(overview.json()["providers"]) == 10, "provider health: 9 providers + Sheets")
            queue = client.get("/api/v1/review-queue").json()["items"]
            check(len(queue) == 1, f"review queue: {[q['company_name'] for q in queue]}")
            blocked = client.post(
                f"/api/v1/review/{queue[0]['company_id']}",
                json={"action": "reject", "reviewer": "x", "reason": "nope"},
            )
            check(blocked.status_code == 403, "review POST without dashboard header refused")

        print("2c. pipeline orchestrator + operator API (Phase 9/10, demo data)")
        demo = asyncio.run(build_demo_outputs(settings))
        pipeline = demo_pipeline(settings, demo)
        run = asyncio.run(pipeline.run(trigger=Trigger.CLI))
        check(
            run.status.value == "succeeded" and run.dry_run, f"dry-run pipeline: {run.status.value}"
        )
        secured = settings.model_copy(update={"operator_api_token": SecretStr("s" * 32)})
        with TestClient(create_app(secured, dashboard_backend=backend)) as client:
            check(
                client.get("/health").status_code == 200, "GET /health open with operator token set"
            )
            denied = client.get("/api/v1/leads")
            check(denied.status_code == 401, f"API without operator token -> {denied.status_code}")
            allowed = client.get("/api/v1/leads", headers={"Authorization": "Bearer " + "s" * 32})
            check(allowed.status_code == 200, f"API with operator token -> {allowed.status_code}")

        print("3. mock pipeline (all 9 active providers, offline)")
        used = asyncio.run(drive_mock_pipeline(ProviderRegistry(settings)))
        # one reasoning provider at a time: the mock registry exercises the default (OpenAI
        # mock); Claude and the deprecated router are selected with REASONING_PROVIDER
        active = set(ProviderName) - {ProviderName.AI_ROUTER, ProviderName.ANTHROPIC}
        check(used == active, f"providers exercised: {sorted(p.value for p in used)}")

        print("4. live adapters refuse to call out (Phase 1 contracts only)")
        # Live adapters must refuse without making ANY call: paid switch off and no local
        # endpoints (the Crawl4AI container is free, so it is not paid-guarded; its traffic
        # would also bypass this process's network guard).
        live_settings = settings.model_copy(
            update={
                "provider_mode": ProviderMode.LIVE,
                "allow_paid_provider_calls": False,
                "crawl4ai_base_url": None,
                "laya_endpoint_url": None,  # local sidecar traffic would bypass the guard
                "enrichment_kit_base_url": None,  # Phase 6 local sidecar: same reason
                "agent_reach_channels": "",  # free research channels would reach the internet
            }
        )
        refused = asyncio.run(live_adapters_refuse(ProviderRegistry(live_settings)))
        check(refused == 9, f"{refused}/9 live adapters refused")

    print("5. network isolation")
    check(attempts == [], f"external network attempts: {attempts or 'none'}")

    print("\nSMOKE CHECK", "FAILED: " + "; ".join(failures) if failures else "PASSED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
