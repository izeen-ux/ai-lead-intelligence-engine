"""Backup API keys (owner decision 2026-10-14): a rejected or used-up key is replaced by the
next one automatically, and the same request is repeated."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from apify_client import errors as apify_errors

from app.core.errors import ProviderCallBlockedError, ProviderError, ProviderNotConfiguredError
from app.core.keyring import KeyRing, ring_for, split_keys
from app.demo import CORE_DEMO_PAGES, build_demo_outputs
from app.providers.contracts import DiscoveryQuery, WebSearchQuery
from app.services.credits import CreditService
from tests.unit.test_apify_adapter import FakeClient, _FakeResponse, make_adapter
from tests.unit.test_website_adapters import firecrawl

K1, K2, K3 = "apify_api_OLD0000", "apify_api_NEW1111", "apify_api_NEW2222"


# ================================================================== the ring itself
def test_ring_order_bench_and_cooldown() -> None:
    now = [0.0]
    ring = KeyRing("apify", ["a", "b", "a", "", "c"], clock=lambda: now[0])
    assert (len(ring), ring.current(), ring.position("b")) == (3, "a", 2)
    assert ring.bench("a", rejected=False) is True and ring.current() == "b"
    assert ring.bench("b", rejected=True) is True and ring.current() == "c"
    assert ring.bench("c", rejected=True) is False  # nothing left to switch to
    assert ring.current() == "a"  # all benched: the provider's own error will be shown
    now[0] = 7 * 3600  # credits may be back after 6 h; a rejected key stays off for 24 h
    assert ring.usable() == ["a"]
    assert ring.bench("unknown", rejected=True) is False


def test_settings_give_primary_then_backups_and_redact_each_key(make_settings) -> None:
    settings = make_settings(apify_token=K1, apify_token_backups=f"{K2}, {K3},{K1}")
    assert settings.provider_keys("apify_token") == [K1, K2, K3]
    assert {K1, K2, K3} <= set(settings.secret_values())  # every key is scrubbed from logs
    assert make_settings().provider_keys("firecrawl_api_key") == []
    assert split_keys(None, " x ,, y") == ["x", "y"]
    assert ring_for("apify", [K1, K2]) is ring_for("apify", [K1, K2])  # one ring per process


# ================================================================== Apify
def apify_settings(make_settings, **kw: Any) -> Any:
    return make_settings(
        provider_mode="live", allow_paid_provider_calls=True, apify_token=K1,
        apify_token_backups=f"{K2},{K3}", apify_max_charge_usd_per_run=Decimal("1.00"), **kw,
    )  # fmt: skip


class SwitchingClient(FakeClient):
    """Fails for the keys in ``bad`` (HTTP status per key), works for the others."""

    def __init__(self, bad: dict[str, int]) -> None:
        super().__init__()
        self.bad = bad

    def actor(self, actor_id: str) -> Any:
        status = self.bad.get(self.tokens[-1])
        self.raise_on_call = (
            apify_errors.ApifyApiError(_FakeResponse(status), 1) if status else None
        )
        return super().actor(actor_id)


async def test_apify_switches_to_the_next_key_and_remembers_it(make_settings) -> None:
    fake = SwitchingClient({K1: 401, K2: 402})  # old key rejected, second one out of credits
    adapter = make_adapter(apify_settings(make_settings), fake)
    result = await adapter.discover(DiscoveryQuery(source="x"))
    assert result.run_ref == "run-abc123" and fake.tokens == [K1, K2, K3]
    fake.tokens.clear()
    await make_adapter(apify_settings(make_settings), fake).discover(DiscoveryQuery(source="x"))
    assert fake.tokens == [K3]  # a new adapter instance starts with the working key


async def test_apify_with_every_key_refused_reports_the_real_error(make_settings) -> None:
    fake = SwitchingClient({K1: 401, K2: 401, K3: 403})
    with pytest.raises(ProviderNotConfiguredError):
        await make_adapter(apify_settings(make_settings), fake).discover(DiscoveryQuery(source="x"))
    assert fake.tokens == [K1, K2, K3]  # each key tried exactly once


async def test_apify_does_not_switch_keys_for_an_ordinary_error(make_settings) -> None:
    fake = SwitchingClient({K1: 404})
    with pytest.raises(ProviderError, match="Apify request failed"):
        await make_adapter(apify_settings(make_settings), fake).discover(DiscoveryQuery(source="x"))
    assert fake.tokens == [K1]


# ================================================================== Firecrawl
def fc_settings(make_settings) -> Any:
    return make_settings(
        provider_mode="live", allow_paid_provider_calls=True,
        firecrawl_api_key="fc-old", firecrawl_api_key_backups="fc-new1,fc-new2",
    )  # fmt: skip


def bearer(request: httpx2.Request) -> str:
    return request.headers["Authorization"].removeprefix("Bearer ")


async def test_firecrawl_switches_on_rejected_and_used_up_keys(make_settings) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        status = {"fc-old": 401, "fc-new1": 402}.get(bearer(request))
        if status:
            return httpx2.Response(status, json={"success": False, "error": "no"})
        return httpx2.Response(200, json={"success": True, "data": {"web": []}})

    adapter, rec = firecrawl(fc_settings(make_settings), handler)
    await adapter.search_web(WebSearchQuery(query="x", limit=5))
    assert [bearer(r) for r in rec.requests] == ["fc-old", "fc-new1", "fc-new2"]
    await adapter.search_web(WebSearchQuery(query="y", limit=5))
    assert bearer(rec.requests[-1]) == "fc-new2" and len(rec.requests) == 4


async def test_firecrawl_last_key_out_of_credits_is_still_an_error(make_settings) -> None:
    adapter, rec = firecrawl(
        fc_settings(make_settings), lambda r: httpx2.Response(402, json={"error": "credits"})
    )
    with pytest.raises(ProviderCallBlockedError, match="credits exhausted"):
        await adapter.search_web(WebSearchQuery(query="x", limit=5))
    assert len(rec.requests) == 3


# ================================================================== credit counter
async def test_credits_add_the_keys_together_and_count_refused_ones(make_settings) -> None:
    def api(request: httpx2.Request) -> httpx2.Response:
        if bearer(request) in (K1, "fc-old"):
            return httpx2.Response(401, json={})
        if "apify" in str(request.url):
            body = {
                "data": {"current": {"monthlyUsageUsd": 1}, "limits": {"maxMonthlyUsageUsd": 5}}
            }
            return httpx2.Response(200, json=body)
        return httpx2.Response(200, json={"data": {"remainingCredits": 900, "planCredits": 1000}})

    settings = make_settings(
        provider_mode="live", apify_token=K1, apify_token_backups=f"{K2},{K3}",
        firecrawl_api_key="fc-old", firecrawl_api_key_backups="fc-new1,fc-new2",
    )  # fmt: skip
    demo = await build_demo_outputs(settings, dict(CORE_DEMO_PAGES))
    report = await CreditService(
        settings, demo.outputs, transport=httpx2.MockTransport(api)
    ).report()
    lines = {line["provider"]: line for line in report["lines"]}
    apify = lines["apify"]
    assert (apify["used"], apify["remaining"], apify["limit"]) == ("2", "8", "10")
    assert apify["note"] == "3 keys added together (1 not accepted by the provider)"
    assert (lines["firecrawl"]["remaining"], lines["firecrawl"]["limit"]) == ("1800", "2000")
    assert K2 not in json.dumps(report) and "fc-new1" not in json.dumps(report)
