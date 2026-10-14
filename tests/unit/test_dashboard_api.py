"""Phase 8: dashboard page + JSON API over the demo backend (no DB, no network)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import ProviderMode
from app.demo import CORE_DEMO_PAGES, DemoOutputs, build_demo_outputs
from app.domain.outputs.leads import lead_id_for
from app.main import create_app
from app.services.dashboard import DashboardBackend

HEADERS = {"X-Dashboard-Request": "1"}
KEY = "hunter_secret_value_never_shown_123"


@pytest.fixture
async def demo(make_settings) -> DemoOutputs:
    return await build_demo_outputs(make_settings(), dict(CORE_DEMO_PAGES))


@pytest.fixture
def client(make_settings, demo) -> Iterator[TestClient]:
    settings = make_settings(hunter_api_key=KEY)
    backend = DashboardBackend(demo.outputs, demo.views, demo.world.qualifications, "demo")
    with TestClient(create_app(settings, dashboard_backend=backend)) as c:
        yield c


def queue(client: TestClient) -> list[dict[str, Any]]:
    return list(client.get("/api/v1/review-queue").json()["items"])


def test_page_is_served(client) -> None:
    resp = client.get("/dashboard")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/html")
    assert "AI Lead Intelligence Engine" in resp.text and "innerHTML" not in resp.text


def test_overview(client) -> None:
    body = client.get("/api/v1/dashboard/overview").json()
    assert body["backend"] == "demo"
    assert body["pipeline"] == {
        "discovered": 3, "verified": 3, "qualified": 2, "review": 1, "not_qualified": 0,
        "enriched": 2, "analyzed": 2, "exported": 0,
    }  # fmt: skip
    names = [p["provider"] for p in body["providers"]]
    assert names == ["apify", "firecrawl", "crawl4ai", "agent_reach", "laya", "enrichment_kit",
                     "hunter", "apollo", "openai", "google_sheets (output)"]  # fmt: skip
    assert all(p["checked"] == "configuration only (no network call)" for p in body["providers"])
    assert {u["basis"] for u in body["usage"]} <= {"provider_reported", "ledger_estimate"}
    assert KEY not in json.dumps(body)


def test_provider_health_makes_no_calls_even_in_live_mode(make_settings, demo) -> None:
    live = make_settings(
        provider_mode=ProviderMode.LIVE,
        allow_paid_provider_calls=True,
        hunter_api_key=KEY,
        apify_token="apify_x",
        firecrawl_api_key="fc_x",
    )
    backend = DashboardBackend(demo.outputs, demo.views, demo.world.qualifications, "demo")
    with TestClient(create_app(live, dashboard_backend=backend)) as c:
        body = c.get("/api/v1/dashboard/overview").json()
    # the hermetic network guard (tests/conftest.py) would fail any outbound call
    modes = {p["provider"]: p["mode"] for p in body["providers"]}
    assert modes["hunter"] == "live" and modes["apify"] == "live"


def test_lists_and_pagination(client) -> None:
    q = queue(client)
    assert [i["company_name"] for i in q] == ["Drivnly"] and q[0]["decision"] == "review"
    qualified = client.get("/api/v1/leads?decision=qualified").json()["items"]
    assert sorted(i["company_name"] for i in qualified) == ["Ecoride Fleet", "Quikcart"]
    assert all(i["export_status"] == "ready" for i in qualified)
    page = client.get("/api/v1/leads?limit=1&offset=1").json()
    assert len(page["items"]) == 1 and page["offset"] == 1
    assert client.get("/api/v1/leads?limit=0").status_code == 422
    assert client.get("/api/v1/leads?decision=maybe").status_code == 422


def test_lead_detail_is_traceable(client, demo) -> None:
    qualified = client.get("/api/v1/leads?decision=qualified").json()["items"][0]
    body = client.get(f"/api/v1/leads/{qualified['company_id']}").json()
    t = body["trace"]
    assert t["company_id"] == qualified["company_id"] and t["qualification_id"]
    assert t["contact_id"] and t["analysis_id"]
    assert body["summary"]["lead_id"] == lead_id_for(__import__("uuid").UUID(t["company_id"]))
    assert body["qualification"]["hard_rules"] and body["qualification"]["events"]
    assert body["analysis"]["offer"]["id"] and body["analysis"]["supporting_evidence_ids"]
    assert body["contact"]["status"] == "verified" and body["contact"]["email"]
    assert body["decision_history"][0]["decision"] == "qualified"
    assert body["export"] is None  # never exported live


def test_unknown_lead_is_404(client) -> None:
    resp = client.get("/api/v1/leads/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "not_found"


# ================================================================== review actions
def test_review_post_requires_the_dashboard_header(client) -> None:
    company = queue(client)[0]["company_id"]
    payload = {"action": "approve", "reviewer": "Asha", "reason": "looks right"}
    resp = client.post(f"/api/v1/review/{company}", json=payload)
    assert resp.status_code == 403
    assert queue(client)[0]["decision"] == "review"  # nothing happened


def test_approve_through_the_api(client) -> None:
    item = queue(client)[0]
    detail = client.get(f"/api/v1/leads/{item['company_id']}").json()
    payload = {"action": "approve", "reviewer": "Asha", "reason": "verified the round",
               "expected_decision_id": detail["trace"]["qualification_id"]}  # fmt: skip
    resp = client.post(f"/api/v1/review/{item['company_id']}", json=payload, headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] and body["new_decision"]["decision"] == "qualified"
    assert queue(client) == []
    after = client.get(f"/api/v1/leads/{item['company_id']}").json()
    assert after["reviews"][0]["reviewer"] == "Asha"
    assert after["qualification"]["human_review"]["reason"] == "verified the round"
    assert [h["decision"] for h in after["decision_history"]][:2] == ["qualified", "review"]
    # replaying the same request is refused as stale - no double decision
    again = client.post(f"/api/v1/review/{item['company_id']}", json=payload, headers=HEADERS)
    assert again.status_code == 409 and again.json()["refusal_code"] == "stale_decision"


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "delete", "reviewer": "a", "reason": "abc"},
        {"action": "approve", "reviewer": "", "reason": "abc"},
        {"action": "approve", "reviewer": "a", "reason": "x"},
        {"action": "approve", "reviewer": "a"},
    ],
)
def test_invalid_review_payloads(client, payload) -> None:
    company = queue(client)[0]["company_id"]
    resp = client.post(f"/api/v1/review/{company}", json=payload, headers=HEADERS)
    assert resp.status_code == 422


def test_refused_action_is_409_and_audited(client) -> None:
    company = queue(client)[0]["company_id"]
    payload = {"action": "reopen", "reviewer": "Asha", "reason": "already in review"}
    resp = client.post(f"/api/v1/review/{company}", json=payload, headers=HEADERS)
    assert resp.status_code == 409 and resp.json()["refusal_code"] == "not_allowed_from_review"
    detail = client.get(f"/api/v1/leads/{company}").json()
    assert detail["reviews"][0]["outcome"] == "refused"


def test_no_mutation_endpoints_besides_review(client) -> None:
    spec = client.get("/openapi.json").json()["paths"]
    writes = sorted(
        path for path, ops in spec.items() if {"post", "put", "patch", "delete"} & set(ops)
    )
    assert writes == [
        "/api/v1/pipeline/recover",
        "/api/v1/pipeline/runs",
        "/api/v1/review/{company_id}",
    ]
