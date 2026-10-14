"""Phase 6 live adapters against the sidecar contract (httpx2.MockTransport - never the network)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx2
import pytest

from app.core.errors import (
    ProviderCallBlockedError,
    ProviderError,
    ProviderMalformedResponseError,
    ProviderNotConfiguredError,
    ProviderQuotaExhaustedError,
    ProviderRunFailedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.domain.enums import (
    EmailVerificationStatus,
    EnrichmentStatus,
    EnrichmentTarget,
    ProviderName,
)
from app.providers.apollo import ApolloAdapter
from app.providers.contracts import EnrichmentRequest, PeopleSearchRequest
from app.providers.enrichment_kit import EnrichmentKitAdapter
from app.providers.hunter import HunterAdapter

URL = "http://127.0.0.1:8787"
HUNTER_KEY = "hunter-secret-value-123"
APOLLO_KEY = "apollo-secret-value-456"
TOKEN = "local-sidecar-token"


def settings(make_settings, **kw: Any):
    base: dict[str, Any] = {
        "provider_mode": "live",
        "allow_paid_provider_calls": True,
        "hunter_api_key": HUNTER_KEY,
        "apollo_api_key": APOLLO_KEY,
        "enrichment_kit_base_url": URL,
        "enrichment_kit_api_token": TOKEN,
    }
    return make_settings(**(base | kw))


class Sidecar:
    def __init__(self, routes: dict[str, Any] | None = None, exc: Exception | None = None):
        self.routes = routes or {}
        self.exc = exc
        self.requests: list[httpx2.Request] = []

    def __call__(self, req: httpx2.Request) -> httpx2.Response:
        self.requests.append(req)
        if self.exc is not None:
            raise self.exc
        status, body = self.routes.get(req.url.path, (404, {"error": "not_found"}))
        if isinstance(body, str):
            return httpx2.Response(status, text=body)
        return httpx2.Response(status, json=body)

    def body(self, i: int = 0) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.requests[i].content)
        return data


def request(**kw: Any) -> EnrichmentRequest:
    base: dict[str, Any] = {
        "lead_id": uuid.uuid4(),
        "target": EnrichmentTarget.PERSON,
        "person_name": "Manav Bansal",
        "first_name": "Manav",
        "last_name": "Bansal",
        "company_name": "Drivn",
        "domain": "drivn.co.in",
        "min_confidence": 0.5,
        "early_exit_confidence": 0.85,
    }
    return EnrichmentRequest(**(base | kw))


def hunter_out(email: str | None = "manav@drivn.co.in", confidence: float = 0.92) -> dict[str, Any]:
    return {
        "email": email, "emailVerified": True, "firstName": "Manav", "lastName": "Bansal",
        "fullName": "Manav Bansal", "title": "CEO", "companyDomain": "drivn.co.in",
        "linkedinUrl": None, "confidence": confidence, "source": "hunter", "cost": 0.004,
        "evidence": {"score": 92, "verification": {"date": "2026-09-20", "status": "valid"}},
    }  # fmt: skip


ENRICH_OK = {
    "success": True,
    "best": hunter_out(),
    "outputs": [{"provider": "hunter", "output": hunter_out(), "error": None, "errorKind": None,
                 "statusCode": None, "latencyMs": 120, "called": True}],
    "totalCost": 0.004,
    "order": ["hunter", "apollo"],
}  # fmt: skip


def kit(make_settings, sidecar: Sidecar, **kw: Any) -> EnrichmentKitAdapter:
    return EnrichmentKitAdapter(
        settings(make_settings, **kw), transport=httpx2.MockTransport(sidecar)
    )


# ================================================================== orchestrator
async def test_enrich_request_shape_and_no_secrets_leave_the_app(make_settings) -> None:
    sidecar = Sidecar({"/v1/enrich": (200, ENRICH_OK)})
    outcome = await kit(make_settings, sidecar).enrich(request(providers=[ProviderName.HUNTER]))
    [req] = sidecar.requests
    assert (req.method, req.url.path) == ("POST", "/v1/enrich")
    assert req.headers["Authorization"] == f"Bearer {TOKEN}"
    sent = sidecar.body()
    assert sent["providers"] == ["hunter"]
    assert sent["input"] == {
        "fullName": "Manav Bansal", "firstName": "Manav", "lastName": "Bansal", "company": "Drivn",
        "domain": "drivn.co.in", "linkedinUrl": None,
    }  # fmt: skip
    assert (sent["minConfidence"], sent["earlyExitOnConfidence"]) == (0.5, 0.85)
    raw = req.content.decode() + str(dict(req.headers))
    assert HUNTER_KEY not in raw and APOLLO_KEY not in raw  # keys live in the sidecar only
    assert outcome.final is not None and outcome.final.email == "manav@drivn.co.in"
    [attempt] = outcome.attempts
    assert attempt.provider is ProviderName.HUNTER
    assert attempt.email_verification is EmailVerificationStatus.UNVERIFIED  # never trusted
    assert attempt.fields["evidence"]["verification"]["status"] == "valid"
    assert attempt.cost_credits == 1


async def test_waterfall_attempts_map_miss_hit_and_typed_errors(make_settings) -> None:
    apollo = {**hunter_out("manav.b@drivn.co.in", 0.9), "source": "apollo",
              "evidence": {"email_status": "verified", "match_confidence": "high"}}  # fmt: skip
    body = {
        "success": True,
        "best": apollo,
        "outputs": [
            {"provider": "hunter", "output": None, "errorKind": "rate_limited", "statusCode": 403,
             "latencyMs": 5, "called": True, "error": "hunter HTTP 403"},
            {"provider": "apollo", "output": apollo, "errorKind": None, "latencyMs": 9,
             "called": True},
        ],
        "totalCost": 0.01,
    }  # fmt: skip
    outcome = await kit(make_settings, Sidecar({"/v1/enrich": (200, body)})).enrich(request())
    hunter, ap = outcome.attempts
    assert (hunter.status, hunter.error_code, hunter.fields["http_status"]) == (
        EnrichmentStatus.FAILED, "rate_limited", 403,
    )  # fmt: skip
    assert hunter.cost_credits == 0
    assert (ap.provider, ap.status, ap.email) == (
        ProviderName.APOLLO,
        EnrichmentStatus.SUCCESS,
        "manav.b@drivn.co.in",
    )
    assert outcome.final is ap


async def test_miss_and_budget_skip_are_distinct(make_settings) -> None:
    body = {"success": False, "best": None, "outputs": [
        {"provider": "hunter", "output": None, "errorKind": None, "called": True},
        {"provider": "apollo", "output": None, "errorKind": "budget_exhausted", "called": False},
    ]}  # fmt: skip
    outcome = await kit(make_settings, Sidecar({"/v1/enrich": (200, body)})).enrich(request())
    assert [a.status for a in outcome.attempts] == [
        EnrichmentStatus.NOT_FOUND,
        EnrichmentStatus.SKIPPED,
    ]
    assert outcome.final is None


@pytest.mark.parametrize(
    "outputs",
    [
        None,
        [{"provider": "clearbit", "output": None}],
        [{"provider": "hunter", "output": {"email": "x@drivn.co.in"}}],  # no confidence
        [{"provider": "hunter", "output": {**hunter_out("not an email"), "confidence": 0.9}}],
        ["nope"],
    ],
)
async def test_malformed_payloads_are_typed(make_settings, outputs) -> None:
    body = {"success": True, "best": None, "outputs": outputs}
    with pytest.raises(ProviderMalformedResponseError):
        await kit(make_settings, Sidecar({"/v1/enrich": (200, body)})).enrich(request())


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (401, {"error": "unauthorized"}, ProviderNotConfiguredError),
        (409, {"error": "no_providers"}, ProviderNotConfiguredError),
        (422, {"error": "bad_request"}, ProviderError),
        (500, {"error": "internal"}, ProviderRunFailedError),
        (200, "<html>", ProviderMalformedResponseError),
        (200, [1, 2], ProviderMalformedResponseError),
    ],
)
async def test_sidecar_http_failures_are_typed(make_settings, status, body, error) -> None:
    with pytest.raises(error):
        await kit(make_settings, Sidecar({"/v1/enrich": (status, body)})).enrich(request())


@pytest.mark.parametrize(
    ("exc", "error"),
    [
        (httpx2.ReadTimeout("slow"), ProviderTimeoutError),
        (httpx2.ConnectError("refused"), ProviderUnavailableError),
    ],
)
async def test_transport_failures_are_typed(make_settings, exc, error) -> None:
    with pytest.raises(error):
        await kit(make_settings, Sidecar(exc=exc)).enrich(request())


async def test_paid_guard_blocks_before_any_request(make_settings) -> None:
    sidecar = Sidecar({"/v1/enrich": (200, ENRICH_OK)})
    adapter = kit(make_settings, sidecar, allow_paid_provider_calls=False)
    with pytest.raises(ProviderCallBlockedError):
        await adapter.enrich(request())
    assert sidecar.requests == []


@pytest.mark.parametrize(
    ("over", "match"),
    [
        ({"enrichment_kit_base_url": None}, "ENRICHMENT_KIT_BASE_URL not set"),
        ({"enrichment_kit_base_url": "http://10.1.2.3:8787"}, "loopback"),
        ({"hunter_api_key": None, "apollo_api_key": None}, "neither"),
    ],
)
async def test_unconfigured_orchestrator_refuses(make_settings, over, match) -> None:
    sidecar = Sidecar({"/v1/enrich": (200, ENRICH_OK)})
    adapter = kit(make_settings, sidecar, **over)
    assert adapter.status().operational is False
    with pytest.raises(ProviderNotConfiguredError, match=match):
        await adapter.enrich(request())
    assert sidecar.requests == []


async def test_health_is_free_and_needs_no_paid_switch(make_settings) -> None:
    sidecar = Sidecar({"/health": (200, {"status": "ok", "providers": {}})})
    adapter = kit(make_settings, sidecar, allow_paid_provider_calls=False)
    assert (await adapter.health())["status"] == "ok"
    assert sidecar.requests[0].method == "GET"
    assert adapter.status().configured is True


async def test_find_people_maps_and_counts_the_credit(make_settings) -> None:
    body = {"provider": "hunter", "errorKind": None, "people": [
        {"firstName": "Manav", "lastName": "Bansal", "position": "CEO", "seniority": "executive",
         "email": "manav@drivn.co.in", "confidence": 94,
         "verification": {"date": "2026-09-01", "status": "valid"}, "sourcesCount": 2},
        "junk",
    ]}  # fmt: skip
    sidecar = Sidecar({"/v1/people": (200, body)})
    result = await kit(make_settings, sidecar).find_people(
        PeopleSearchRequest(domain="www.drivn.co.in", limit=3)
    )
    assert sidecar.body() == {"domain": "drivn.co.in", "limit": 3}
    [p] = result.people
    assert (p.first_name, p.confidence, p.verification_status) == ("Manav", 0.94, "valid")
    assert result.credits_used == 1
    empty = await kit(make_settings, Sidecar({"/v1/people": (200, {"people": []})})).find_people(
        PeopleSearchRequest(domain="drivn.co.in")
    )
    assert empty.credits_used == 0
    with pytest.raises(ProviderQuotaExhaustedError):
        await kit(
            make_settings, Sidecar({"/v1/people": (200, {"errorKind": "quota_exhausted"})})
        ).find_people(PeopleSearchRequest(domain="drivn.co.in"))


# ================================================================== Hunter / Apollo via sidecar
def hunter(make_settings, sidecar: Sidecar, **kw: Any) -> HunterAdapter:
    return HunterAdapter(settings(make_settings, **kw), transport=httpx2.MockTransport(sidecar))


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("valid", EmailVerificationStatus.VALID),
        ("invalid", EmailVerificationStatus.INVALID),
        ("accept_all", EmailVerificationStatus.ACCEPT_ALL),
        ("webmail", EmailVerificationStatus.WEBMAIL),
        ("disposable", EmailVerificationStatus.DISPOSABLE),
        ("pending", EmailVerificationStatus.PENDING),
        ("weird", EmailVerificationStatus.UNKNOWN),
    ],
)
async def test_verification_statuses(make_settings, status, expected) -> None:
    body = {"provider": "hunter", "email": "Manav@Drivn.co.in", "status": status, "score": 97,
            "errorKind": None, "httpStatus": 200}  # fmt: skip
    sidecar = Sidecar({"/v1/verify": (200, body)})
    result = await hunter(make_settings, sidecar).verify_email("manav@drivn.co.in")
    assert sidecar.body() == {"email": "manav@drivn.co.in"}
    assert (result.status, result.email, result.score) == (expected, "manav@drivn.co.in", 0.97)


@pytest.mark.parametrize("kind", ["timeout", "rate_limited", "pii_blocked"])
async def test_per_address_verification_failures_are_recorded_not_raised(
    make_settings, kind
) -> None:
    body = {
        "provider": "hunter",
        "email": "a@drivn.co.in",
        "status": None,
        "errorKind": kind,
        "httpStatus": None,
    }
    result = await hunter(make_settings, Sidecar({"/v1/verify": (200, body)})).verify_email(
        "a@drivn.co.in"
    )
    assert (result.status, result.error_code) == (EmailVerificationStatus.UNKNOWN, kind)


@pytest.mark.parametrize(
    ("kind", "error"),
    [("auth", ProviderNotConfiguredError), ("quota_exhausted", ProviderQuotaExhaustedError)],
)
async def test_account_level_verification_failures_raise(make_settings, kind, error) -> None:
    body = {"provider": "hunter", "email": "a@drivn.co.in", "errorKind": kind}
    with pytest.raises(error):
        await hunter(make_settings, Sidecar({"/v1/verify": (200, body)})).verify_email(
            "a@drivn.co.in"
        )


async def test_vendor_adapters_route_one_provider_through_the_sidecar(make_settings) -> None:
    sidecar = Sidecar({"/v1/enrich": (200, ENRICH_OK)})
    result = await hunter(make_settings, sidecar).enrich(request())
    assert sidecar.body()["providers"] == ["hunter"]
    assert result.email == "manav@drivn.co.in"
    apollo_sidecar = Sidecar({"/v1/enrich": (200, {"outputs": []})})
    apollo = ApolloAdapter(settings(make_settings), transport=httpx2.MockTransport(apollo_sidecar))
    miss = await apollo.enrich(request())
    assert apollo_sidecar.body()["providers"] == ["apollo"]
    assert miss.status is EnrichmentStatus.NOT_FOUND


async def test_vendor_status_and_guards(make_settings) -> None:
    no_key = hunter(make_settings, Sidecar(), hunter_api_key=None)
    assert no_key.status().configured is False
    assert "HUNTER_API_KEY" in (no_key.status().detail or "")
    no_url = hunter(make_settings, Sidecar(), enrichment_kit_base_url=None)
    with pytest.raises(ProviderNotConfiguredError, match="ENRICHMENT_KIT_BASE_URL"):
        await no_url.verify_email("a@drivn.co.in")
    blocked = hunter(make_settings, Sidecar(), allow_paid_provider_calls=False)
    with pytest.raises(ProviderCallBlockedError):
        await blocked.verify_email("a@drivn.co.in")


async def test_account_usage_is_free_and_needs_no_paid_switch(make_settings) -> None:
    body = {"provider": "hunter", "planName": "Free", "requests": {"searches": {"used": 1}}}
    sidecar = Sidecar({"/v1/account": (200, body)})
    adapter = kit(make_settings, sidecar, allow_paid_provider_calls=False)
    assert (await adapter.account_usage())["requests"] == {"searches": {"used": 1}}
    assert sidecar.requests[0].url.path == "/v1/account"
    with pytest.raises(ProviderNotConfiguredError):
        await kit(make_settings, Sidecar(), enrichment_kit_base_url=None).account_usage()
