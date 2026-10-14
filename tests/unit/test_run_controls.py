"""Dashboard run controls: operator prompt -> search queries, lead owners, one tab per 500
leads, and the credit counter (provider counters via a fake transport - never the network)."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx2
import pytest

from app.core.errors import ConfigurationError
from app.demo import CORE_DEMO_PAGES, build_demo_outputs
from app.domain.outputs.leads import LEADS_COLUMNS, lead_cells
from app.domain.outputs.records import ExportMode
from app.domain.pipeline import Stage, Trigger
from app.providers.google_sheets import InMemorySheetsWriter
from app.repositories.pipeline import InMemoryPipelineStore
from app.services.credits import CreditService
from app.services.pipeline import (
    PipelineService,
    owner_names,
    prompt_queries,
    resolve_owner,
    run_summary,
)
from app.services.pipeline_stages import discovery_query
from app.services.sheets_export import SheetsExportService, sheet_tabs

OWNERS = "Asha,Ravi,Meera"


# ================================================================== prompt
def test_prompt_becomes_one_query_per_line() -> None:
    prompt = (
        "  D2C skincare brand raises seed funding  \n\n"
        "AI startup India   angel round\nAI startup India angel round"
    )
    assert prompt_queries(prompt) == [
        "D2C skincare brand raises seed funding",
        "AI startup India angel round",
    ]
    assert prompt_queries(None) == prompt_queries("  \n ") == []
    assert len(prompt_queries("\n".join(f"q{i}" for i in range(30)))) == 10


def test_prompt_replaces_the_configured_queries_for_every_source(make_settings) -> None:
    settings = make_settings(apify_max_items_per_run=3)
    mine = ["skincare brand raises seed", "beauty startup funding"]
    news = discovery_query(settings, "funding_news", limit=10, prompt_queries=mine)
    social = discovery_query(settings, "linkedin_posts", limit=10, prompt_queries=mine, rotation=1)
    assert news is not None and news.keywords == mine
    assert social is not None and social.keywords == ["beauty startup funding"]
    default = discovery_query(settings, "funding_news", limit=10)
    assert default is not None and default.keywords != mine  # empty prompt -> saved defaults


# ================================================================== owners
def test_owner_must_be_one_of_the_team() -> None:
    assert owner_names(OWNERS) == ["Asha", "Ravi", "Meera"]
    assert resolve_owner(" meera ", OWNERS) == "Meera"  # canonical spelling
    assert resolve_owner(None, OWNERS) is None and resolve_owner("", OWNERS) is None
    with pytest.raises(ConfigurationError, match="unknown lead owner"):
        resolve_owner("Someone Else", OWNERS)
    assert resolve_owner("Anyone", "") == "Anyone"  # no list configured


def test_run_records_owner_and_prompt(make_settings) -> None:
    settings = make_settings(lead_owners=OWNERS)
    assert make_settings().lead_owners == ""  # no team list by default: any owner name works
    service = PipelineService(InMemoryPipelineStore(), {}, settings)
    run = service.new_run(
        trigger=Trigger.API, owner="ravi", prompt="fintech raises seed\nagritech raises seed"
    )
    assert run.config["owner"] == "Ravi"
    assert run.config["discovery_queries"] == ["fintech raises seed", "agritech raises seed"]
    summary = run_summary(run)
    assert summary["owner"] == "Ravi" and "fintech" in str(summary["prompt"])
    plain = service.new_run(trigger=Trigger.CLI)
    assert plain.config["owner"] is None and plain.config["discovery_queries"] == []
    with pytest.raises(ConfigurationError):
        service.new_run(trigger=Trigger.API, owner="Nobody")


async def test_owner_is_a_sheet_column(make_settings) -> None:
    demo = await build_demo_outputs(make_settings(), dict(CORE_DEMO_PAGES))
    cid = demo.world.company_ids[0]
    ctx = demo.world.enrichment.companies[cid]
    demo.world.enrichment.companies[cid] = type(ctx)(**{**ctx.__dict__, "owner": "Meera"})
    view = demo.views.view(cid)
    assert view is not None and view.owner == "Meera"
    row = dict(zip(LEADS_COLUMNS, lead_cells(view, _catalog()), strict=True))
    assert LEADS_COLUMNS[-1] == "lead_owner" and row["lead_owner"] == "Meera"


def _catalog() -> Any:
    from app.domain.analysis.catalog import default_catalog

    return default_catalog()


# ================================================================== one tab per N leads
def test_tabs_are_found_in_fill_order() -> None:
    tabs = ["Sheet1", "Ai Leads 3", "Ai Leads", "Ai Leads 2", "Ai Leads old", "Ai Leads 1"]
    assert sheet_tabs("Ai Leads", tabs) == ["Ai Leads", "Ai Leads 2", "Ai Leads 3"]
    assert sheet_tabs("Ai Leads", []) == ["Ai Leads"]


async def test_a_full_tab_starts_the_next_one_and_leads_never_move(make_settings) -> None:
    settings = make_settings(google_sheets_spreadsheet_id="1abc", export_rows_per_sheet=2)
    demo = await build_demo_outputs(settings, dict(CORE_DEMO_PAGES))  # 3 rows for the sheet
    sheet = InMemorySheetsWriter()

    def export() -> SheetsExportService:
        return SheetsExportService(demo.outputs, demo.views, settings, sheet, mode=ExportMode.MOCK)

    first = (await export().run()).run
    assert list(sheet.tabs) == ["Ai Leads", "Ai Leads 2"]
    assert (len(sheet.keys("Ai Leads")), len(sheet.keys("Ai Leads 2"))) == (2, 1)
    assert sheet.tabs["Ai Leads 2"][0] == list(LEADS_COLUMNS)  # the new tab gets the header
    assert first.inserted_count == 3 and first.worksheets == ["Ai Leads", "Ai Leads 2"]
    before = {name: [list(r) for r in rows] for name, rows in sheet.tabs.items()}
    again = (await export().run()).run
    assert (again.inserted_count, again.updated_count, again.unchanged_count) == (0, 0, 3)
    assert sheet.tabs == before  # nothing moved between tabs, nothing duplicated
    keys = sheet.keys("Ai Leads") + sheet.keys("Ai Leads 2")
    assert len(keys) == len(set(keys)) == 3


async def test_default_tab_size_is_500(make_settings) -> None:
    assert make_settings().export_rows_per_sheet == 500


# ================================================================== credit counter
def provider_api(request: httpx2.Request) -> httpx2.Response:
    assert request.headers["Authorization"].startswith("Bearer ")
    if "apify" in str(request.url):
        body = {"data": {"current": {"monthlyUsageUsd": 0.25}, "limits": {"maxMonthlyUsageUsd": 5}}}
        return httpx2.Response(200, json=body)
    if "credit-usage" in str(request.url):
        return httpx2.Response(200, json={"data": {"remainingCredits": 870, "planCredits": 1000}})
    return httpx2.Response(404)


async def hunter_usage() -> dict[str, Any]:
    return {"provider": "hunter", "errorKind": None, "requests": {
        "searches": {"used": 11, "available": 50}, "verifications": {"used": 21, "available": 100},
    }}  # fmt: skip


async def credits(make_settings, **kw: Any) -> dict[str, Any]:
    settings = make_settings(apify_token="apify_api_x", firecrawl_api_key="fc-x", **kw)
    demo = await build_demo_outputs(settings, dict(CORE_DEMO_PAGES))
    service = CreditService(
        settings, demo.outputs, transport=httpx2.MockTransport(provider_api),
        hunter_usage=hunter_usage,
    )  # fmt: skip
    return await service.report()


async def test_live_mode_reads_the_providers_own_counters(make_settings) -> None:
    report = await credits(make_settings, provider_mode="live")
    lines = {(line["provider"], line["unit"]): line for line in report["lines"]}
    assert report["live"] is True
    apify = lines[("apify", "USD")]
    assert (apify["used"], apify["remaining"], apify["limit"]) == ("0.25", "4.75", "5")
    assert apify["source"] == "provider"
    fire = lines[("firecrawl", "credits")]
    assert (fire["used"], fire["remaining"], fire["limit"]) == ("130", "870", "1000")
    assert lines[("hunter", "searches")]["remaining"] == "39"
    assert lines[("hunter", "verifications")]["remaining"] == "79"
    openai = lines[("openai", "USD")]
    assert openai["source"] == "app" and Decimal(openai["limit"]) == Decimal(5)
    assert "apify_api_x" not in json.dumps(report) and "fc-x" not in json.dumps(report)
    assert {"screened", "skipped", "estimated_credits_saved"} <= set(report["screening"])


async def test_safe_mode_makes_no_provider_call(make_settings) -> None:
    def explode(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("no provider call is allowed in safe mode")

    settings = make_settings(apify_token="apify_api_x", firecrawl_api_key="fc-x")
    demo = await build_demo_outputs(settings, dict(CORE_DEMO_PAGES))
    report = await CreditService(
        settings, demo.outputs, transport=httpx2.MockTransport(explode)
    ).report()
    assert report["live"] is False
    apify = next(line for line in report["lines"] if line["provider"] == "apify")
    assert apify["remaining"] is None and "live mode" in apify["note"]


async def test_an_unreadable_provider_is_reported_not_guessed(make_settings) -> None:
    async def broken() -> dict[str, Any]:
        return {"provider": "hunter", "errorKind": "auth_failed"}

    settings = make_settings(provider_mode="live", apify_token="apify_api_x")
    demo = await build_demo_outputs(settings, dict(CORE_DEMO_PAGES))
    service = CreditService(
        settings, demo.outputs,
        transport=httpx2.MockTransport(lambda request: httpx2.Response(500)), hunter_usage=broken,
    )  # fmt: skip
    lines = (await service.report())["lines"]
    by = {line["provider"]: line for line in lines}
    assert by["apify"]["remaining"] is None and "could not be read" in by["apify"]["note"]
    assert by["hunter"]["remaining"] is None and "could not be read" in by["hunter"]["note"]
    assert by["firecrawl"]["note"] == "FIRECRAWL_API_KEY not set"


def test_stage_enum_still_has_eight_stages_for_the_run_button() -> None:
    assert [s.value for s in Stage] == [
        "discovery", "prescreen", "website", "signals", "qualification", "enrichment",
        "analysis", "export",
    ]  # fmt: skip


def test_per_run_credit_rows_show_what_the_pre_screen_saved() -> None:
    from app.services.credits import run_credit_rows

    runs = [
        {"run_id": "a", "started_at": "2026-10-05T15:30:00+00:00", "owner": "Meera", "stages": [
            {"stage": "prescreen", "counts": {"companies": 20, "scrape": 10, "skip": 10}},
            {"stage": "website", "counts": {"companies": 10, "credits": 80, "verified": 4}},
            {"stage": "qualification", "counts": {"qualified": 2}},
        ]},
        {"run_id": "b", "requested_by": "cli", "stages": [
            {"stage": "prescreen", "counts": {"companies": 4, "scrape": 0, "skip": 4}},
        ]},
        {"run_id": "c", "stages": [{"stage": "export", "counts": {"leads": 7}}]},  # no scraping
    ]  # fmt: skip
    first, second = run_credit_rows(runs, Decimal("8.1"))
    assert (first["screened"], first["stopped_before_scraping"], first["scraped"]) == (20, 10, 10)
    assert (first["website_credits"], first["credits_per_company"]) == (80, "8.0")
    assert first["credits_saved_estimate"] == "80" and first["qualified"] == 2
    assert first["owner"] == "Meera"
    # nothing scraped in this run: the overall average prices what was saved
    assert (second["scraped"], second["website_credits"]) == (0, 0)
    assert second["credits_saved_estimate"] == "32" and second["owner"] == "cli"


# ================================================================== credits per run
def test_credit_delta_is_after_minus_before_and_never_guesses() -> None:
    from app.services.credits import credit_delta

    before = [
        {"provider": "apify", "unit": "USD", "used": "0.40", "remaining": "4.60", "limit": "5"},
        {"provider": "firecrawl", "unit": "credits", "used": None, "remaining": "850"},
        {"provider": "hunter", "unit": "searches", "used": None, "remaining": None},
    ]
    after = [
        {"provider": "apify", "unit": "USD", "used": "0.52", "remaining": "4.48", "limit": "5"},
        {"provider": "firecrawl", "unit": "credits", "used": None, "remaining": "801"},
        {"provider": "hunter", "unit": "searches", "used": "3", "remaining": "47"},
    ]
    rows = {r["provider"]: r for r in credit_delta(before, after)}
    assert (rows["apify"]["spent"], rows["apify"]["remaining"]) == ("0.12", "4.48")
    assert rows["firecrawl"]["spent"] == "49"  # from the drop of "remaining"
    assert rows["hunter"]["spent"] is None  # unreadable before the run -> unknown, not 0
    assert credit_delta(None, after) == [] and credit_delta(before, None) == []


async def test_a_writing_run_records_what_it_spent(make_settings) -> None:
    readings = iter(
        [
            [{"provider": "firecrawl", "unit": "credits", "used": "100", "remaining": "900"}],
            [{"provider": "firecrawl", "unit": "credits", "used": "113", "remaining": "887"}],
        ]
    )

    async def snapshot() -> list[dict[str, Any]]:
        return next(readings)

    async def broken() -> list[dict[str, Any]]:
        raise RuntimeError("provider down")

    store = InMemoryPipelineStore()
    service = PipelineService(
        store, {}, make_settings(), allow_mock_writes=True, credit_snapshot=snapshot
    )
    run = await service.run(trigger=Trigger.API, dry_run=False, stages=[Stage.EXPORT])
    spent: Any = run_summary(run)["credits"]
    assert [(c["spent"], c["remaining"]) for c in spent] == [("13", "887")]
    dry = await service.run(trigger=Trigger.API, stages=[Stage.EXPORT])  # dry run: no reading
    assert run_summary(dry)["credits"] == []
    failing = PipelineService(
        InMemoryPipelineStore(), {}, make_settings(), allow_mock_writes=True, credit_snapshot=broken
    )
    ok = await failing.run(trigger=Trigger.API, dry_run=False, stages=[Stage.EXPORT])
    assert ok.status.value == "succeeded" and run_summary(ok)["credits"] == []


# ================================================================== plain-language prompt
def test_a_sentence_prompt_becomes_search_queries() -> None:
    """Regression (2026-10-12): the sentence was sent to the search engine word for word and
    found no funding news."""
    queries = prompt_queries(
        "early stage startup got funding till 5cr and trying to expnad the team"
    )
    assert "startup raises Rs 5 crore" in queries and "startup raises seed round India" in queries
    assert any("expand team" in q for q in queries)
    assert not any("expnad" in q or "5cr" in q for q in queries)  # typos never reach a search
    sector = prompt_queries("I need healthtech startups who raised funding recently")
    assert sector[0] == "healthtech startup raises pre-seed funding India"
    small = prompt_queries("we want startups that got funding upto 2 crore")
    assert "startup raises Rs 2 crore" in small and "startup raises Rs 3 crore" not in small
    assert len(prompt_queries("i need leads\nwe want fintech startups")) <= 10


# ================================================================== leads wanted (rounds)
def round_runners(inserted: list[int], new_companies: int = 3) -> dict[Stage, Any]:
    from app.services.pipeline import StageOutcome

    left = iter(inserted)

    async def discovery(ctx: Any) -> Any:
        return StageOutcome(counts={"new_companies": new_companies, "candidates": 4})

    async def export(ctx: Any) -> Any:
        return StageOutcome(counts={"inserted": next(left, 0), "leads": 9, "unchanged": 7})

    return {Stage.DISCOVERY: discovery, Stage.EXPORT: export}


def rounds_service(make_settings, inserted: list[int], **kw: Any) -> PipelineService:
    return PipelineService(
        InMemoryPipelineStore(),
        round_runners(inserted, **kw),
        make_settings(pipeline_max_rounds=4),
        allow_mock_writes=True,
    )


STAGES = [Stage.DISCOVERY, Stage.EXPORT]


async def test_a_run_repeats_until_the_wanted_leads_are_found(make_settings) -> None:
    service = rounds_service(make_settings, [1, 0, 2, 5])
    run = await service.run(trigger=Trigger.API, dry_run=False, stages=STAGES, target_leads=3)
    summary = run_summary(run)
    assert (summary["round"], summary["leads_found"], summary["target_leads"]) == (3, 3, 3)
    assert summary["stop_reason"] == "the wanted number of leads was found"
    assert [s.stage for s in run.stages] == STAGES  # one merged result per stage
    export = run.result(Stage.EXPORT)
    assert export is not None and export.counts == {"inserted": 3, "leads": 9, "unchanged": 7}
    found = run.result(Stage.DISCOVERY)
    assert found is not None and found.counts["new_companies"] == 9  # rounds add up


async def test_rounds_stop_at_the_round_limit_and_say_so(make_settings) -> None:
    service = rounds_service(make_settings, [0, 1, 0, 0, 9])
    run = await service.run(trigger=Trigger.API, dry_run=False, stages=STAGES, target_leads=5)
    summary = run_summary(run)
    assert run.status.value == "succeeded" and summary["round"] == 4
    assert summary["leads_found"] == 1 and "4 search rounds" in str(summary["stop_reason"])


async def test_rounds_stop_when_no_company_is_left(make_settings) -> None:
    service = rounds_service(make_settings, [0, 0, 0, 0], new_companies=0)
    run = await service.run(trigger=Trigger.API, dry_run=False, stages=STAGES, target_leads=5)
    assert run_summary(run)["round"] == 2
    assert "no more new companies" in str(run_summary(run)["stop_reason"])


async def test_without_leads_wanted_a_run_is_one_round(make_settings) -> None:
    service = rounds_service(make_settings, [0, 5])
    run = await service.run(trigger=Trigger.API, dry_run=False, stages=STAGES)
    assert run_summary(run)["round"] == 1 and run_summary(run)["stop_reason"] is None
    with pytest.raises(ConfigurationError, match="leads wanted"):
        service.new_run(trigger=Trigger.API, target_leads=99)


# ================================================================== reddit
def test_reddit_keeps_its_subreddit_searches_when_a_prompt_is_given(make_settings) -> None:
    """Regression (2026-10-13): news headlines were sent to Reddit as keywords - 213 posts,
    17 company names - and without the Actor's own limit a run billed 100 posts, not 10."""
    from app.providers.apify.social import RedditSearchSpec
    from app.services.pipeline_stages import reddit_queries

    configured = ["StartUpIndia:raised seed funding", "indianstartups:raised funding"]
    headlines = ["startup raises Rs 5 crore", "startup raises seed round India"]
    assert reddit_queries(configured, headlines) == configured
    sector = reddit_queries(configured, ["healthtech startup raises Rs 2 crore"])
    assert sector[0] == "StartUpIndia:healthtech raised seed funding"
    assert reddit_queries(configured, ["developersIndia:hiring founding engineer"]) == [
        "developersIndia:hiring founding engineer"
    ]
    query = discovery_query(make_settings(), "reddit", limit=10, prompt_queries=headlines)
    assert query is not None and ":" in query.keywords[0]
    sent = RedditSearchSpec().build_input(query)
    assert sent["maxPosts"] == make_settings().discovery_social_max_items_per_run
    assert sent["subredditName"] and "raises Rs 5 crore" not in sent["subredditKeywords"][0]
