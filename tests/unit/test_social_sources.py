"""Social / job-platform discovery sources: LinkedIn posts, LinkedIn jobs, X - parsed from
real (trimmed) Actor output captured 2026-10-06; never the network."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.core.errors import SourceNotRunnableError
from app.domain.enums import SourceType
from app.domain.sources import SOURCES, SourceRegistry
from app.providers.apify.actors import actor_specs
from app.providers.apify.adapter import ApifyAdapter
from app.providers.apify.social import (
    LinkedInJobsSpec,
    LinkedInPostSearchSpec,
    RedditSearchSpec,
    XPostSearchSpec,
    companies_in_post,
)
from app.providers.contracts import DiscoveryQuery
from app.services.pipeline_stages import discovery_query

ITEMS: dict[str, list[dict[str, Any]]] = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "social_actor_items.json").read_text(
        encoding="utf-8"
    )
)
SOCIAL = ("linkedin_posts", "linkedin_jobs", "x_posts", "reddit")


# ------------------------------------------------------------------ registry / policy
def test_social_sources_are_approved_cookie_free_and_off_by_default() -> None:
    assert SourceRegistry().runnable() == ["funding_news"]
    enabled = SourceRegistry.from_setting("funding_news," + ",".join(SOCIAL))
    assert enabled.runnable() == [
        "funding_news", "linkedin_jobs", "linkedin_posts", "reddit", "x_posts",
    ]  # fmt: skip
    specs = actor_specs()
    for source in SOCIAL:
        definition = SOURCES[source]
        assert definition.auth_required is False and definition.actor_id in specs
        assert "no login" in definition.policy_notes.lower()


@pytest.mark.parametrize("source", ["linkedin", "x_twitter", "youtube"])
def test_login_gated_or_unverified_sources_still_cannot_run(source: str) -> None:
    with pytest.raises(SourceNotRunnableError):
        SourceRegistry.from_setting(f"funding_news,{source}").require_runnable(source)


@pytest.mark.parametrize("spec", [LinkedInPostSearchSpec(), LinkedInJobsSpec(), XPostSearchSpec()])
def test_inputs_never_carry_cookies_or_paid_extras(spec: Any) -> None:
    sent = spec.build_input(DiscoveryQuery(source="x", keywords=["seed funding"], max_items=7))
    blob = json.dumps(sent).lower()
    assert not any(word in blob for word in ("cookie", "session", "password", "login", "token"))
    assert 7 in sent.values()  # the item limit is always part of the input
    assert sent.get("scrapeReactions", False) is False
    assert sent.get("scrapeComments", False) is False


# ------------------------------------------------------------------ budgets
def test_query_plan_uses_one_rotating_query_and_the_social_item_budget(make_settings) -> None:
    settings = make_settings(discovery_linkedin_queries="a;b", discovery_social_max_items_per_run=8)
    first = discovery_query(settings, "linkedin_posts", limit=10)
    second = discovery_query(settings, "linkedin_posts", limit=10, rotation=1)
    assert first is not None and second is not None
    assert (first.keywords, second.keywords) == (["a"], ["b"])
    assert first.max_items == 8 and first.params == {}  # no Google-only date parameter
    assert first.actor_id == "harvestapi/linkedin-post-search"
    search = discovery_query(settings, "funding_news", limit=10)
    assert search is not None and search.max_items is None and search.params


def test_item_ceiling_and_cost_estimate_fit_the_default_budget(make_settings) -> None:
    settings = make_settings(provider_mode="live", allow_paid_provider_calls=True)
    adapter = ApifyAdapter(settings)
    for source in SOCIAL:
        query = discovery_query(settings, source, limit=10)
        assert query is not None
        spec = adapter._spec(query)
        items, _ = adapter.effective_limits(query.model_copy(update={"max_items": 500}), spec)
        assert items == settings.discovery_social_max_items_per_run == 10
        estimate = spec.estimate_cost_usd(spec.build_input(query))
        assert Decimal(0) < estimate <= settings.apify_max_estimated_cost_usd_per_run
    google = discovery_query(settings, "funding_news", limit=10)
    assert google is not None
    assert adapter.effective_limits(google, adapter._spec(google))[0] == 1  # pages, unchanged


# ------------------------------------------------------------------ parsers (real output)
def test_linkedin_posts_yield_funded_companies() -> None:
    batch = LinkedInPostSearchSpec().normalize(
        ITEMS["linkedin_posts"], DiscoveryQuery(source="linkedin_posts", keywords=["q"])
    )
    names = [c.company_name for c in batch.candidates]
    assert {"Zaperon", "Scooter", "Gravity"} <= set(names) and batch.skipped == 2
    scooter = next(c for c in batch.candidates if c.company_name == "Scooter")
    assert scooter.source_type is SourceType.SOCIAL and scooter.source_platform == "linkedin.com"
    assert scooter.raw["funding_mention"] == "Rs 1.5 crore" and scooter.published_at is not None
    assert scooter.raw["discovery_source"] == "linkedin_posts"
    assert "author" not in json.dumps(scooter.raw)  # no profile data of the poster is kept


def test_x_posts_yield_funded_companies_and_skip_noise() -> None:
    batch = XPostSearchSpec().normalize(ITEMS["x_posts"], DiscoveryQuery(source="x_posts"))
    names = {c.company_name for c in batch.candidates}
    assert {"NapTapGo", "Dextr AI", "Fixxly", "BUILT"} <= names
    assert "Agrani" not in names  # "eyes Rs 850 Crore" is not a completed round
    tap = next(c for c in batch.candidates if c.company_name == "NapTapGo")
    assert tap.raw["funding_mention"] == "₹8 Cr"
    assert str(tap.source_url).startswith("https://x.com/")
    reply = [{**ITEMS["x_posts"][2], "isReply": True}]
    assert XPostSearchSpec().normalize(reply, DiscoveryQuery(source="x_posts")).candidates == []


def test_linkedin_jobs_carry_the_company_website_and_filter_by_country_and_size() -> None:
    spec = LinkedInJobsSpec()
    batch = spec.normalize(ITEMS["linkedin_jobs"], DiscoveryQuery(source="linkedin_jobs"))
    names = [c.company_name for c in batch.candidates]
    assert names == ["CodeRound AI", "TechKareer", "SaaS Ventures"]
    code = batch.candidates[0]
    assert code.domain == "coderound.ai" and code.source_type is SourceType.JOB_BOARD
    assert code.raw["employee_count"] == 15 and code.raw["country"] == "IN"
    assert code.source_title == "CodeRound AI is hiring: Founding Engineer (Up to 100LPA)"
    assert batch.skipped == 2  # a US company (twice)
    big = [{**ITEMS["linkedin_jobs"][0], "companyEmployeesCount": 5000}]
    assert spec.normalize(big, DiscoveryQuery(source="linkedin_jobs")).candidates == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("🚀 NapTapGo raises ₹8 Cr ($800K) in seed funding led by IPV.", ["NapTapGo"]),
        ("Gaming tech startup @PlayARCX1 raises ₹10.5 cr in pre-seed funding", ["PlayARCX1"]),
        (
            "Delhi-based cybersecurity startup Zaperon, co-founded by A B and C D, has raised "
            "₹7 crore in a seed funding round",
            ["Zaperon"],
        ),
        (
            "1. #Gravity raised $15M from investors\n2. #Acme bags Rs 2 crore seed round",
            ["Gravity", "Acme"],
        ),
        ("We are hiring! Join our growing team in Pune.", []),
        ("A salon owner in Bhopal was paying someone ₹15,000 a month", []),
        ("", []),
    ],
)
def test_companies_in_post(text: str, expected: list[str]) -> None:
    assert [name for name, _ in companies_in_post(text)] == expected


# ------------------------------------------------------------------ reddit
def test_reddit_search_stays_inside_one_startup_subreddit() -> None:
    spec = RedditSearchSpec()
    sent = spec.build_input(
        DiscoveryQuery(source="reddit", keywords=["r/indianstartups:raised funding"], max_items=6)
    )
    assert sent["subredditName"] == "indianstartups"
    assert sent["subredditKeywords"] == ["raised funding"] and sent["scrapeComments"] is False
    assert spec.estimate_cost_usd(sent) == 6 * spec.price_item_usd
    bare = spec.build_input(DiscoveryQuery(source="reddit", keywords=["seed funding"]))
    assert bare["subredditName"] == "StartUpIndia"  # never a search across all of Reddit


def test_reddit_posts_yield_only_clear_funding_announcements() -> None:
    batch = RedditSearchSpec().normalize(ITEMS["reddit"], DiscoveryQuery(source="reddit"))
    assert [c.company_name for c in batch.candidates] == ["Acme Pay"]
    assert batch.skipped == 2  # unrelated posts are ignored, not guessed at
    acme = batch.candidates[0]
    assert acme.source_platform == "reddit.com" and acme.raw["funding_mention"] == "Rs 2 crore"
    adult = [{**ITEMS["reddit"][2], "over_18": True}]
    assert RedditSearchSpec().normalize(adult, DiscoveryQuery(source="reddit")).candidates == []


async def test_estimate_hint_is_not_sent_to_the_actor(make_settings) -> None:
    from tests.unit.test_apify_adapter import FakeClient

    settings = make_settings(
        provider_mode="live", allow_paid_provider_calls=True, apify_token="apify_api_test"
    )
    fake = FakeClient()
    fake.items = []
    adapter = ApifyAdapter(settings, client_factory=lambda token: fake)
    query = discovery_query(settings, "reddit", limit=5)
    assert query is not None
    result = await adapter.discover(query)
    assert "_items" not in result.provider_input and "subredditName" in result.provider_input
