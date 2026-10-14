"""Apify live adapter + Actor spec, exercised with a fake client (never the network)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from apify_client import errors as apify_errors

from app.core.errors import (
    DomainValidationError,
    ProviderCallBlockedError,
    ProviderError,
    ProviderNotConfiguredError,
    ProviderRunFailedError,
    ProviderUnavailableError,
)
from app.domain.enums import ProviderName, SourceType
from app.providers.apify.actors import (
    GOOGLE_SEARCH_VERIFIED_INPUT_KEYS,
    GoogleSearchFundingNewsSpec,
)
from app.providers.apify.adapter import ApifyAdapter
from app.providers.contracts import DiscoveryQuery

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "google_search_item.json"


def sample_item() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


# ---------------------------------------------------------------- fake apify-client
@dataclass
class FakeRun:
    id: str = "run-abc123"
    status: str = "SUCCEEDED"
    default_dataset_id: str = "ds-xyz"
    started_at: datetime = field(default_factory=lambda: datetime(2026, 9, 28, tzinfo=UTC))
    finished_at: datetime | None = field(
        default_factory=lambda: datetime(2026, 9, 28, 0, 0, 20, tzinfo=UTC)
    )
    usage_total_usd: float | None = 0.0055
    charged_event_counts: dict[str, int] | None = field(
        default_factory=lambda: {"actor-start": 1, "search-page-scraped": 1}
    )


class FakeActor:
    def __init__(self, parent: FakeClient) -> None:
        self.parent = parent

    async def start(self, **kwargs: Any) -> Any:
        self.parent.call_kwargs = kwargs
        if self.parent.raise_on_call is not None:
            raise self.parent.raise_on_call
        return SimpleNamespace(id=self.parent.started_id, status="RUNNING")


class FakeDataset:
    def __init__(self, parent: FakeClient) -> None:
        self.parent = parent

    async def _gen(self, limit: int | None) -> AsyncIterator[dict[str, Any]]:
        for item in self.parent.items[:limit]:
            yield item

    def iterate_items(self, *, limit: int | None = None) -> AsyncIterator[dict[str, Any]]:
        self.parent.dataset_limit = limit
        return self._gen(limit)


class FakeClient:
    def __init__(self) -> None:
        self.run_result: FakeRun | None = FakeRun()
        self.items: list[dict[str, Any]] = [sample_item()]
        self.raise_on_call: BaseException | None = None
        self.call_kwargs: dict[str, Any] = {}
        self.actor_ids: list[str] = []
        self.tokens: list[str] = []
        self.dataset_limit: int | None = None
        self.refreshed_run: FakeRun | None = None
        self.started_id = "run-abc123"
        self.wait_kwargs: dict[str, Any] = {}
        self.events: list[str] = []

    def actor(self, actor_id: str) -> FakeActor:
        self.actor_ids.append(actor_id)
        return FakeActor(self)

    def dataset(self, dataset_id: str) -> FakeDataset:
        return FakeDataset(self)

    def run(self, run_id: str) -> FakeRunClient:
        return FakeRunClient(self)


class FakeRunClient:
    def __init__(self, parent: FakeClient) -> None:
        self.parent = parent

    async def get(self) -> Any:
        return self.parent.refreshed_run

    async def wait_for_finish(self, **kwargs: Any) -> Any:
        self.parent.wait_kwargs = kwargs
        self.parent.events.append("wait")
        return self.parent.run_result


@pytest.fixture
def fake() -> FakeClient:
    return FakeClient()


@pytest.fixture
def live_settings(make_settings):
    return make_settings(
        provider_mode="live",
        allow_paid_provider_calls=True,
        apify_token="apify_api_TESTTOKENVALUE0000000000000000000",
        apify_max_items_per_run=2,
        apify_max_charge_usd_per_run=Decimal("1.00"),
    )


def make_adapter(settings, fake: FakeClient) -> ApifyAdapter:
    def factory(token: str) -> FakeClient:
        fake.tokens.append(token)
        return fake

    return ApifyAdapter(settings, client_factory=factory)


# ---------------------------------------------------------------- spec: input
def test_input_uses_only_verified_keys_and_pins_paid_addons_off() -> None:
    spec = GoogleSearchFundingNewsSpec()
    actor_input = spec.build_input(
        DiscoveryQuery(source="funding_news", country_codes=["IN"], params={"quickDateRange": "m1"})
    )
    assert set(actor_input) <= GOOGLE_SEARCH_VERIFIED_INPUT_KEYS
    assert actor_input["queries"] == "startup raises seed funding crore India"
    assert actor_input["countryCode"] == "in"
    assert actor_input["quickDateRange"] == "m1"
    assert actor_input["maxPagesPerQuery"] == 1
    # paid add-ons and storage side effects are explicitly disabled
    assert actor_input["maximumLeadsEnrichmentRecords"] == 0
    assert actor_input["saveHtmlToKeyValueStore"] is False  # Actor default is TRUE
    assert actor_input["websiteContentScraper"] == {"enable": False}
    assert actor_input["aiOverview"] == {"scrapeFullAiOverview": False}
    assert actor_input["focusOnPaidAds"] is False


def test_input_rejects_unsupported_params() -> None:
    with pytest.raises(DomainValidationError, match="unsupported"):
        GoogleSearchFundingNewsSpec().build_input(
            DiscoveryQuery(source="x", params={"maximumLeadsEnrichmentRecords": 5})
        )


def test_keywords_become_one_query_per_line() -> None:
    actor_input = GoogleSearchFundingNewsSpec().build_input(
        DiscoveryQuery(source="x", keywords=[" a b ", "", "c d"])
    )
    assert actor_input["queries"] == "a b\nc d"
    assert actor_input["countryCode"] == ""


# ---------------------------------------------------------------- spec: normalization
def test_normalize_maps_funding_headlines_and_skips_the_rest() -> None:
    batch = GoogleSearchFundingNewsSpec().normalize(
        [sample_item()], DiscoveryQuery(source="funding_news")
    )
    names = [c.company_name for c in batch.candidates]
    assert names == ["Acme Robotics", "Globex Pay", "ACME Robotics Pvt Ltd"]
    assert batch.skipped == 3  # roundup, how-to, non-funding headline

    first = batch.candidates[0]
    assert first.domain is None  # publisher URL is NOT the company domain
    assert first.source_type is SourceType.NEWS
    assert first.source_platform == "news.example"
    assert first.published_at == datetime(2026, 9, 20, tzinfo=UTC)
    assert first.raw["funding_mention"] == "Rs 3 Cr"
    assert first.raw["funding_round"] == "seed"
    assert first.raw["position"] == 1
    assert first.raw["query_term"] == "startup raises seed funding crore India"
    assert batch.candidates[1].published_at is None  # no date in result


def test_normalize_tolerates_missing_and_malformed_fields() -> None:
    items = [
        {},
        {"organicResults": None},
        {"organicResults": [{"title": "Acme raises Rs 2 Cr seed"}]},  # no url
        {"organicResults": [{"title": "Acme raises Rs 2 Cr seed", "url": "not a url"}]},
        {
            "organicResults": [
                {"title": "Zeta raises Rs 2 Cr", "url": "https://z.example", "date": "x"}
            ]
        },
    ]
    batch = GoogleSearchFundingNewsSpec().normalize(items, DiscoveryQuery(source="x"))
    assert [c.company_name for c in batch.candidates] == ["Zeta"]
    assert batch.skipped == 2


# ---------------------------------------------------------------- adapter: guards
async def test_paid_guard_blocks_before_client_is_created(make_settings, fake) -> None:
    s = make_settings(provider_mode="live", apify_token="apify_api_x" * 3)
    adapter = make_adapter(s, fake)
    with pytest.raises(ProviderCallBlockedError):
        await adapter.discover(DiscoveryQuery(source="funding_news"))
    assert fake.tokens == []  # client never built, no call made


async def test_missing_token_is_reported(make_settings, fake) -> None:
    s = make_settings(provider_mode="live", allow_paid_provider_calls=True)
    with pytest.raises(ProviderNotConfiguredError):
        await make_adapter(s, fake).discover(DiscoveryQuery(source="x"))
    assert fake.tokens == []


async def test_unknown_actor_is_rejected(live_settings, fake) -> None:
    with pytest.raises(DomainValidationError, match="no Actor spec"):
        await make_adapter(live_settings, fake).discover(
            DiscoveryQuery(source="x", actor_id="someone/unverified-actor")
        )
    assert fake.actor_ids == []


# ---------------------------------------------------------------- adapter: limits
@pytest.mark.parametrize(
    ("requested_items", "requested_charge", "expected_items", "expected_charge"),
    [
        (None, None, 2, Decimal("1.00")),  # ceilings
        (1, Decimal("0.60"), 1, Decimal("0.60")),  # lower request honoured
        (99, Decimal(10), 2, Decimal("1.00")),  # higher request clamped
    ],
)
async def test_limits_are_clamped_to_ceilings(
    live_settings, fake, requested_items, requested_charge, expected_items, expected_charge
) -> None:
    result = await make_adapter(live_settings, fake).discover(
        DiscoveryQuery(source="x", max_items=requested_items, max_charge_usd=requested_charge)
    )
    assert fake.call_kwargs["max_items"] == expected_items
    assert fake.call_kwargs["max_total_charge_usd"] == expected_charge
    assert fake.dataset_limit == expected_items
    assert result.max_items == expected_items
    assert result.max_charge_usd == expected_charge


# ---------------------------------------------------------------- adapter: success
async def test_successful_run_returns_metadata_and_candidates(live_settings, fake) -> None:
    result = await make_adapter(live_settings, fake).discover(
        DiscoveryQuery(source="funding_news", limit=2)
    )
    assert fake.actor_ids == ["apify/google-search-scraper"]
    assert "logger" not in fake.call_kwargs  # start() does not stream Actor logs
    assert fake.call_kwargs["run_timeout"] == timedelta(seconds=180)
    assert fake.wait_kwargs["wait_duration"] == timedelta(seconds=240)
    assert result.provider is ProviderName.APIFY
    assert result.actor_id == "apify/google-search-scraper"
    assert result.run_ref == "run-abc123"
    assert result.provider_status == "SUCCEEDED"
    assert result.cost_usd == Decimal("0.0055")
    assert result.charged_events == {"actor-start": 1, "search-page-scraped": 1}
    assert result.raw_item_count == 1
    assert len(result.candidates) == 2  # limit applied
    assert result.skipped_count == 3 + 1  # non-candidates + one over the limit
    assert result.provider_input["maximumLeadsEnrichmentRecords"] == 0


async def test_run_id_is_reported_before_waiting_for_the_run(live_settings, fake) -> None:
    """D1: the hook fires with the run id before the adapter blocks on the run."""

    async def hook(run_id: str) -> None:
        fake.events.append(f"started:{run_id}")

    await make_adapter(live_settings, fake).discover(
        DiscoveryQuery(source="x"), on_run_started=hook
    )
    assert fake.events == ["started:run-abc123", "wait"]


async def test_wait_window_expiry_carries_the_run_id(live_settings, fake) -> None:
    fake.run_result = None  # wait_for_finish returned without a finished run
    with pytest.raises(ProviderUnavailableError) as exc:
        await make_adapter(live_settings, fake).discover(DiscoveryQuery(source="x"))
    assert exc.value.details["run_id"] == "run-abc123"


async def test_token_never_leaks_into_result_or_logs(live_settings, fake, capsys) -> None:
    result = await make_adapter(live_settings, fake).discover(DiscoveryQuery(source="x"))
    token = "apify_api_TESTTOKENVALUE0000000000000000000"
    assert fake.tokens == [token]  # handed to the client only
    assert token not in result.model_dump_json()
    assert token not in capsys.readouterr().out


# ---------------------------------------------------------------- adapter: failures
@pytest.mark.parametrize("status", ["FAILED", "TIMED-OUT", "ABORTED"])
async def test_unsuccessful_run_raises_with_run_id_and_cost(live_settings, fake, status) -> None:
    assert fake.run_result is not None
    fake.run_result.status = status
    fake.items = [] if status == "TIMED-OUT" else fake.items  # timed out with nothing stored
    with pytest.raises(ProviderRunFailedError) as exc_info:
        await make_adapter(live_settings, fake).discover(DiscoveryQuery(source="x"))
    assert exc_info.value.details["run_id"] == "run-abc123"
    assert exc_info.value.details["status"] == status
    assert exc_info.value.details["cost_usd"] == "0.0055"


async def test_run_not_finished_in_wait_window(live_settings, fake) -> None:
    fake.run_result = None
    with pytest.raises(ProviderUnavailableError, match="wait window"):
        await make_adapter(live_settings, fake).discover(DiscoveryQuery(source="x"))


class _FakeResponse:
    """Just enough of an HTTP response for ``ApifyApiError`` to build itself."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.text = json.dumps({"error": {"type": "mock", "message": "mock api error"}})
        self.headers: dict[str, str] = {"content-type": "application/json"}

    def json(self) -> Any:
        return json.loads(self.text)


@pytest.mark.parametrize(
    ("error_cls", "status", "expected"),
    [
        (apify_errors.UnauthorizedError, 401, ProviderNotConfiguredError),
        (apify_errors.ForbiddenError, 403, ProviderNotConfiguredError),
        (apify_errors.RateLimitError, 429, ProviderUnavailableError),
        (apify_errors.ServerError, 500, ProviderUnavailableError),
        (apify_errors.NotFoundError, 404, ProviderError),
    ],
)
async def test_client_errors_are_mapped(live_settings, fake, error_cls, status, expected) -> None:
    error = apify_errors.ApifyApiError(_FakeResponse(status), 1)
    assert isinstance(error, error_cls)  # the client's own status -> class dispatch
    fake.raise_on_call = error
    with pytest.raises(expected) as exc_info:
        await make_adapter(live_settings, fake).discover(DiscoveryQuery(source="x"))
    assert exc_info.value.details["provider"] == "apify"
    assert exc_info.value.details["http_status"] == status


async def test_network_errors_are_retryable(live_settings, fake) -> None:
    fake.raise_on_call = TimeoutError()
    with pytest.raises(ProviderUnavailableError) as exc_info:
        await make_adapter(live_settings, fake).discover(DiscoveryQuery(source="x"))
    assert exc_info.value.retryable is True


def test_run_info_reads_typed_run_duck_typed() -> None:
    info = ApifyAdapter._run_info(
        SimpleNamespace(
            id="r1",
            status="SUCCEEDED",
            default_dataset_id="d1",
            started_at=None,
            finished_at=None,
            usage_total_usd=None,
            charged_event_counts=None,
        )
    )
    assert info["cost_usd"] is None
    assert info["charged_events"] == {}


def test_default_client_factory_builds_official_client() -> None:
    from apify_client import ApifyClientAsync

    from app.providers.apify.adapter import _default_client_factory

    assert isinstance(_default_client_factory("dummy-token"), ApifyClientAsync)


# ---------------------------------------------------------------- cost controls (Phase 2 fix)
async def test_charge_cap_below_apify_minimum_is_rejected_before_calling(
    live_settings, fake
) -> None:
    with pytest.raises(DomainValidationError, match=r"below \$0.50"):
        await make_adapter(live_settings, fake).discover(
            DiscoveryQuery(source="x", max_charge_usd=Decimal("0.02"))
        )
    assert fake.actor_ids == []


def test_estimate_is_actor_start_plus_pages() -> None:
    spec = GoogleSearchFundingNewsSpec()
    one = spec.build_input(DiscoveryQuery(source="x"))
    three = spec.build_input(DiscoveryQuery(source="x", keywords=["a", "b", "c"]))
    assert spec.estimate_cost_usd(one) == Decimal("0.0055")
    assert spec.estimate_cost_usd(three) == Decimal("0.0145")


async def test_estimate_over_budget_blocks_before_calling(make_settings, fake) -> None:
    s = make_settings(
        provider_mode="live",
        allow_paid_provider_calls=True,
        apify_token="apify_api_x" * 3,
        apify_max_estimated_cost_usd_per_run=Decimal("0.01"),
    )
    with pytest.raises(ProviderCallBlockedError, match="estimated worst-case cost") as exc_info:
        await make_adapter(s, fake).discover(DiscoveryQuery(source="x", keywords=["a", "b", "c"]))
    assert exc_info.value.details["estimated_cost_usd"] == "0.0145"
    assert fake.actor_ids == []


async def test_estimate_is_reported_on_success(live_settings, fake) -> None:
    result = await make_adapter(live_settings, fake).discover(DiscoveryQuery(source="x"))
    assert result.estimated_cost_usd == Decimal("0.0055")


async def test_api_error_message_is_surfaced_and_token_scrubbed(live_settings, fake) -> None:
    token = "apify_api_TESTTOKENVALUE0000000000000000000"
    response = _FakeResponse(400)
    response.text = json.dumps(
        {"error": {"type": "invalid-input", "message": f"Bad input for {token}: minimum $0.50"}}
    )
    fake.raise_on_call = apify_errors.ApifyApiError(response, 1)
    with pytest.raises(ProviderError) as exc_info:
        await make_adapter(live_settings, fake).discover(DiscoveryQuery(source="x"))
    err = exc_info.value
    assert "minimum $0.50" in err.message
    assert token not in err.message
    assert token not in json.dumps(err.details)
    assert err.details["api_error_type"] == "invalid-input"


async def test_final_cost_is_refreshed_after_dataset_read(live_settings, fake) -> None:
    # Live observation: at SUCCEEDED only actor-start was billed; the page event settled later.
    assert fake.run_result is not None
    fake.run_result.usage_total_usd = 0.001
    fake.run_result.charged_event_counts = {"actor-start": 1, "search-page-scraped": 0}
    fake.refreshed_run = FakeRun(
        usage_total_usd=0.0055, charged_event_counts={"actor-start": 1, "search-page-scraped": 1}
    )
    result = await make_adapter(live_settings, fake).discover(DiscoveryQuery(source="x"))
    assert result.cost_usd == Decimal("0.0055")
    assert result.charged_events["search-page-scraped"] == 1


async def test_refresh_returning_nothing_keeps_original_cost(live_settings, fake) -> None:
    fake.refreshed_run = None
    result = await make_adapter(live_settings, fake).discover(DiscoveryQuery(source="x"))
    assert result.cost_usd == Decimal("0.0055")


# ---------------------------------------------------------------- network drops (2026-10-12)
class LibraryConnectError(Exception):
    """Stands for the HTTP library's own error type - NOT an OSError (that crashed a run)."""


async def test_a_short_network_drop_while_waiting_is_retried(
    live_settings, fake, monkeypatch
) -> None:
    from app.providers.apify import adapter as module

    monkeypatch.setattr(module, "WAIT_RETRY_SECONDS", 0)
    calls = {"n": 0}

    async def flaky(self: FakeRunClient, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise LibraryConnectError("connection dropped")
        return fake.run_result

    monkeypatch.setattr(FakeRunClient, "wait_for_finish", flaky)
    result = await make_adapter(live_settings, fake).discover(DiscoveryQuery(source="x"))
    assert calls["n"] == 2 and result.run_ref == "run-abc123"  # the paid results are kept


async def test_a_lasting_network_failure_is_a_provider_error_not_a_crash(
    live_settings, fake, monkeypatch
) -> None:
    from app.providers.apify import adapter as module

    monkeypatch.setattr(module, "WAIT_RETRY_SECONDS", 0)

    async def down(self: FakeRunClient, **kwargs: Any) -> Any:
        raise LibraryConnectError("no route")

    monkeypatch.setattr(FakeRunClient, "wait_for_finish", down)
    with pytest.raises(ProviderUnavailableError, match="LibraryConnectError"):
        await make_adapter(live_settings, fake).discover(DiscoveryQuery(source="x"))


async def test_a_timed_out_run_keeps_the_results_it_already_stored(live_settings, fake) -> None:
    """Seen live 2026-10-12: the search Actor hit its time limit after one paid page and the
    whole source was reported failed."""
    assert fake.run_result is not None
    fake.run_result.status = "TIMED-OUT"
    result = await make_adapter(live_settings, fake).discover(DiscoveryQuery(source="x"))
    assert result.provider_status == "TIMED-OUT" and result.raw_item_count == 1
