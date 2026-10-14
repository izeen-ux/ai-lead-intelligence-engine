"""Final lead-output expansion: multi-query / multi-source discovery under explicit budgets,
the ``startup_news`` registry source, and the sources that must stay blocked."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.errors import SourceNotRunnableError
from app.domain.sources import SOURCES, SourceRegistry
from app.providers.apify.actors import GoogleSearchFundingNewsSpec
from app.providers.contracts import DiscoveryQuery
from app.services.pipeline_stages import discovery_queries, discovery_query
from app.utils.funding_headlines import extract_company_from_news_headline


def page(*titles: str) -> dict[str, object]:
    return {
        "searchQuery": {"term": "q", "page": 1},
        "organicResults": [
            {"title": t, "url": f"https://news.example/{i}", "description": ""}
            for i, t in enumerate(titles)
        ],
    }


# ------------------------------------------------------------------ query planning
def test_default_query_set_targets_the_icp_and_is_plural(make_settings) -> None:
    queries = discovery_queries(make_settings(), "funding_news")
    assert len(queries) >= 5 and len(set(queries)) == len(queries)
    assert all("seed" in q or "angel" in q or "bridge" in q for q in queries)


def test_conservative_default_sends_one_query(make_settings) -> None:
    query = discovery_query(make_settings(), "funding_news", limit=10)
    assert query is not None and len(query.keywords) == 1
    assert query.limit == 10 and query.country_codes == ["IN"]
    assert query.params == {"quickDateRange": "m3"}  # the 90-day trigger window


def test_item_budget_decides_how_many_queries_run(make_settings) -> None:
    settings = make_settings(apify_max_items_per_run=4)
    query = discovery_query(settings, "funding_news", limit=10)
    assert query is not None and len(query.keywords) == 4
    # worst-case estimate stays inside the default Apify estimate budget
    spec = GoogleSearchFundingNewsSpec()
    estimate = spec.estimate_cost_usd(spec.build_input(query))
    assert estimate == Decimal("0.019") <= settings.apify_max_estimated_cost_usd_per_run


def test_consecutive_runs_rotate_through_the_query_set(make_settings) -> None:
    settings = make_settings(discovery_queries="a;b;c", apify_max_items_per_run=2)
    picks = [
        discovery_query(settings, "funding_news", limit=5, rotation=n).keywords  # type: ignore[union-attr]
        for n in range(4)
    ]
    # each run takes the NEXT block of queries (a "leads wanted" run repeats in rounds)
    assert picks == [["a", "b"], ["c", "a"], ["b", "c"], ["a", "b"]]


def test_no_queries_configured_means_no_request(make_settings) -> None:
    settings = make_settings(discovery_queries=" ; ", discovery_quick_date_range="")
    assert discovery_query(settings, "funding_news", limit=5) is None
    assert discovery_query(make_settings(), "linkedin", limit=5) is None


def test_deprecated_keyword_override_still_works(make_settings) -> None:
    settings = make_settings(pipeline_discovery_keywords="one, two")
    assert discovery_queries(settings, "funding_news") == ["one", "two"]
    assert discovery_queries(settings, "startup_news") != ["one", "two"]


# ------------------------------------------------------------------ candidates per run
def test_several_result_pages_yield_several_independent_companies() -> None:
    spec = GoogleSearchFundingNewsSpec()
    query = DiscoveryQuery(source="funding_news", keywords=["a", "b"], limit=10)
    batch = spec.normalize(
        [
            page("Acme Pay raises Rs 3 Cr in seed round", "Top 10 funding rounds this week"),
            page("Kheti Labs bags Rs 2 crore pre-seed funding", "Zenly secures seed capital"),
        ],
        query,
    )
    assert [c.company_name for c in batch.candidates] == ["Acme Pay", "Kheti Labs", "Zenly"]
    assert batch.skipped == 1


def test_startup_news_source_reads_growth_headlines() -> None:
    spec = GoogleSearchFundingNewsSpec()
    titles = ("Bengaluru-based startup Kheti Labs launches AI advisory", "How startups hire")
    funding = spec.normalize([page(*titles)], DiscoveryQuery(source="funding_news"))
    news = spec.normalize([page(*titles)], DiscoveryQuery(source="startup_news"))
    assert funding.candidates == []
    assert [c.company_name for c in news.candidates] == ["Kheti Labs"]
    assert news.candidates[0].raw["discovery_source"] == "startup_news"
    assert "launches" in spec.build_input(DiscoveryQuery(source="startup_news"))["queries"]


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Zepto expands to 10 new cities", "Zepto"),
        ("Acme raises Rs 3 Cr in seed round", "Acme"),
        ("Why startups launch too early", None),
        ("Top startups hiring this month", None),
        ("a quiet week for launches", None),
    ],
)
def test_news_headline_names_are_conservative(title: str, expected: str | None) -> None:
    assert extract_company_from_news_headline(title) == expected


# ------------------------------------------------------------------ registry policy
def test_startup_news_is_approved_but_off_by_default() -> None:
    assert SourceRegistry().runnable() == ["funding_news"]
    both = SourceRegistry.from_setting("funding_news,startup_news")
    assert both.runnable() == ["funding_news", "startup_news"]
    assert SOURCES["startup_news"].auth_required is False


@pytest.mark.parametrize("source", ["linkedin", "x_twitter", "youtube", "job_boards"])
def test_login_or_unreviewed_sources_cannot_be_enabled(source: str) -> None:
    registry = SourceRegistry.from_setting(f"funding_news,{source}")
    assert source not in registry.runnable()
    with pytest.raises(SourceNotRunnableError):
        registry.require_runnable(source)


# ------------------------------------------------------------------ noise seen in the live run
@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Factrika has raised ₹8.9 crore in seed funding", "Factrika"),  # was "Factrika has"
        ("Firi Funding: Beauty Quick Commerce Startup Raises $3 Mn", "Firi"),
        ("Seed Funding Advisors in India raise the bar for capital", None),
        ("Looking to launch your startup in India?", None),
        ("Ecosystem expands as Indian startups grow", None),
        ("MeitY just launched a startup platform", "MeitY"),
        ("Exclusive: Acme Pay raises Rs 3 Cr in seed round", "Acme Pay"),
        ("Bengaluru-based fintech startup Acme Pay raises Rs 3 Cr seed round", "Acme Pay"),
        ("UpKraft raises ₹1.6 Crore pre-seed round", "UpKraft"),
    ],
)
def test_headline_names_drop_filler_labels_and_generic_subjects(
    title: str, expected: str | None
) -> None:
    assert extract_company_from_news_headline(title) == expected
