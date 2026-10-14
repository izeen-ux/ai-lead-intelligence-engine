"""Agent-Reach adapter: planning, RSS/yt-dlp parsing, policy filtering, budgets, error mapping.

No network: RSS via httpx2.MockTransport, yt-dlp via an injected fake runner.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pydantic import ValidationError

from app.core.errors import (
    ProviderChannelUnsupportedError,
    ProviderMalformedResponseError,
    ProviderPolicyBlockedError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.domain.enums import ResearchChannel, SignalType
from app.domain.signals import (
    DISABLED_CHANNELS,
    classify,
    context_terms_from,
    mentions_company,
    score,
)
from app.providers.agent_reach.adapter import AgentReachAdapter
from app.providers.contracts import SignalResearchRequest

RSS = (Path(__file__).resolve().parents[1] / "fixtures" / "google_news_rss.xml").read_bytes()

YT_LINES = "\n".join(
    [
        json.dumps(
            {
                "id": "2Ck183UsVK4",
                "url": "https://www.youtube.com/watch?v=2Ck183UsVK4",
                "title": "Financing Electric Trucks | Manav Bansal, Drivn - commercial EV leasing",
                "channel": "Founder Thesis",
                "timestamp": 1790000000,
            }
        ),
        json.dumps(
            {
                "url": "https://www.youtube.com/watch?v=zzz",
                "title": "Unrelated cooking video",
                "channel": "Cook",
                "upload_date": "20260901",
            }
        ),
        "WARNING: some noise line",
    ]
)


class FakeRunner:
    def __init__(
        self, code: int = 0, out: str = YT_LINES, err: str = "", exc: BaseException | None = None
    ) -> None:
        self.code, self.out, self.err, self.exc = code, out, err, exc
        self.calls: list[list[str]] = []

    async def __call__(self, args: list[str], limit_seconds: float) -> tuple[int, str, str]:
        self.calls.append(args)
        if self.exc:
            raise self.exc
        return self.code, self.out, self.err


class RssHandler:
    def __init__(self, status: int = 200, body: bytes = RSS, exc: BaseException | None = None):
        self.status, self.body, self.exc = status, body, exc
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.exc:
            raise self.exc
        return httpx2.Response(self.status, content=self.body)


def adapter(
    make_settings, rss: RssHandler | None = None, runner: FakeRunner | None = None, **kw: Any
) -> tuple[AgentReachAdapter, RssHandler, FakeRunner]:
    rss = rss or RssHandler()
    runner = runner or FakeRunner()
    settings = make_settings(provider_mode="live", **kw)
    return (
        AgentReachAdapter(settings, transport=httpx2.MockTransport(rss), runner=runner),
        rss,
        runner,
    )


def request(**kw: Any) -> SignalResearchRequest:
    base: dict[str, Any] = {
        "company_name": "Drivn",
        "normalized_name": "drivn",
        "domain": "drivn.co.in",
        "context_terms": ["leasing", "commercial"],
        "max_queries": 5,
        "max_results": 20,
    }
    return SignalResearchRequest(**(base | kw))


# ================================================================== pure signal rules
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Drivn raises Rs 45 Cr in seed funding", {SignalType.FUNDING}),
        ("Drivn is hiring engineers", {SignalType.HIRING}),
        ("Drivn expands to Pune", {SignalType.EXPANSION}),
        ("Drivn adopts AI automation for fleet ops", {SignalType.AI_INTEREST}),
        ("Drivn faces charging delays", {SignalType.PAIN_POINT}),
        ("Drivn wins award", set()),
    ],
)
def test_classify(text: str, expected: set[SignalType]) -> None:
    assert set(classify(text)) == expected


def test_mentions_company_is_whole_word() -> None:
    assert mentions_company("Drivn's entity raises money", "drivn")
    assert not mentions_company("Drivnet launches", "drivn")
    assert mentions_company("Dextr-AI raises", "dextr ai")
    assert not mentions_company("anything", "   ")


def test_score_bounds_and_evidence() -> None:
    s = score(
        "Drivn commercial EV leasing raises funding, see drivn.co.in",
        channel=ResearchChannel.NEWS_RSS,
        normalized_name="drivn",
        domain="drivn.co.in",
        context_terms=["leasing"],
    )
    assert s is not None
    assert 0 <= s.confidence <= 0.95
    assert any("verified domain" in n for n in s.evidence_notes)
    assert (
        score(
            "Other co raises",
            channel=ResearchChannel.NEWS_RSS,
            normalized_name="drivn",
            domain=None,
            context_terms=[],
        )
        is None
    )


def test_context_terms() -> None:
    assert context_terms_from(
        "Drivn Transition Pvt Ltd | E-mobility Solution | Commercial EV Leasing"
    ) == ["drivn", "transition", "mobility", "commercial", "leasing"]


def test_context_terms_exclude_company_name_words() -> None:
    """D4: the company's own name is not a disambiguating context term."""
    title = "Drivn Transition Pvt Ltd | E-mobility Solution | Commercial EV Leasing"
    assert context_terms_from(title, company_name="Drivn") == [
        "transition",
        "mobility",
        "commercial",
        "leasing",
    ]
    assert "robotics" not in context_terms_from(
        "Acme Robotics | Warehouse AI", company_name="Acme Robotics"
    )


def test_youtube_query_never_repeats_the_company_name() -> None:
    """D4: live run produced 'Drivn drivn' -> irrelevant videos. Name words are skipped."""
    plan = AgentReachAdapter.plan_queries(
        request(context_terms=["drivn", "leasing"]), [ResearchChannel.YOUTUBE]
    )
    assert plan == [(ResearchChannel.YOUTUBE, "Drivn leasing")]
    only_name = AgentReachAdapter.plan_queries(
        request(context_terms=["drivn"]), [ResearchChannel.YOUTUBE]
    )
    assert only_name == [(ResearchChannel.YOUTUBE, "Drivn")]


def test_policy_disabled_channels_documented() -> None:
    for forbidden in ("linkedin", "instagram", "twitter_x", "reddit", "facebook"):
        assert forbidden in DISABLED_CHANNELS
    assert {c.value for c in ResearchChannel} == {"news_rss", "youtube"}


@pytest.mark.parametrize("bad", ["linkedin", "news_rss,instagram", "twitter"])
def test_settings_refuse_non_permitted_channels(make_settings, bad: str) -> None:
    with pytest.raises(ValidationError, match="non-permitted"):
        make_settings(agent_reach_channels=bad)


def test_settings_normalize_channels(make_settings) -> None:
    s = make_settings(agent_reach_channels=" YouTube , news_rss,youtube ")
    assert s.research_channels() == [ResearchChannel.YOUTUBE, ResearchChannel.NEWS_RSS]


# ================================================================== planning / budget
def test_plan_interleaves_channels_and_respects_query_budget() -> None:
    both = [ResearchChannel.NEWS_RSS, ResearchChannel.YOUTUBE]
    plan = AgentReachAdapter.plan_queries(request(max_queries=3), both)
    assert plan == [
        (ResearchChannel.NEWS_RSS, '"Drivn"'),
        (ResearchChannel.YOUTUBE, "Drivn leasing"),
        (ResearchChannel.NEWS_RSS, '"Drivn" funding OR raises'),
    ]
    assert len(AgentReachAdapter.plan_queries(request(max_queries=10), both)) == 6
    topical = AgentReachAdapter.plan_queries(
        request(questions=["hiring"]), [ResearchChannel.NEWS_RSS]
    )
    assert topical == [
        (ResearchChannel.NEWS_RSS, '"Drivn"'),
        (ResearchChannel.NEWS_RSS, '"Drivn" hiring'),
    ]


async def test_item_budget_caps_processing_across_queries(make_settings) -> None:
    ar, rss, runner = adapter(make_settings)
    result = await ar.research(request(max_results=4))
    assert result.items_processed == 4
    assert sum(q.items_processed for q in result.queries) == 4
    assert len(rss.requests) == 1  # budget exhausted after the first query
    assert runner.calls == []


# ================================================================== RSS parsing + policy
async def test_rss_research_end_to_end(make_settings) -> None:
    ar, rss, _ = adapter(make_settings, agent_reach_channels="news_rss")
    result = await ar.research(request(max_queries=1))

    params = dict(rss.requests[0].url.params)
    assert params == {"q": '"Drivn"', "hl": "en-IN", "gl": "IN", "ceid": "IN:en"}
    assert rss.requests[0].url.host == "news.google.com"
    assert "cookie" not in {k.lower() for k in rss.requests[0].headers}

    by_type = {(i.signal_type, i.title) for i in result.items}
    assert (
        SignalType.FUNDING,
        "Exclusive: Drivn's Indian entity raises Rs 45 Cr from Avaana Capital",
    ) in by_type
    assert (
        SignalType.HIRING,
        "Drivn is hiring fleet operations and AI automation engineers for commercial EV leasing",
    ) in by_type
    assert (
        SignalType.EXPANSION,
        "Ravindra Energy Associate EIM Signs MoU with Drivn for 1,000 Electric Trucks",
    ) in by_type
    assert not any("linkedin" in str(i.url) for i in result.items)  # social dropped by policy
    assert not any("Top EV startups" in (i.title or "") for i in result.items)  # not about company

    funding = next(i for i in result.items if i.signal_type is SignalType.FUNDING)
    assert funding.platform == "news:Entrackr"
    assert funding.channel == "news_rss"
    assert funding.query == '"Drivn"'
    assert funding.published_at == datetime(2026, 9, 21, 10, 1, 19, tzinfo=UTC)
    assert funding.snippet == funding.title  # verbatim evidence
    assert 0 < funding.confidence <= 0.95
    hiring = next(i for i in result.items if i.signal_type is SignalType.HIRING)
    assert hiring.confidence > funding.confidence  # matched verified-site context terms
    assert result.items_processed == 6  # 7 items, one without title skipped at parse
    assert result.items_dropped >= 3
    assert result.channels_used == ["news_rss"]
    assert result.channels_disabled["youtube"] == "not selected in AGENT_REACH_CHANNELS"
    assert "linkedin" in result.channels_disabled


async def test_rss_bad_date_is_none_not_invented(make_settings) -> None:
    ar, _, _ = adapter(make_settings, agent_reach_channels="news_rss")
    result = await ar.research(request(max_queries=1))
    assert all(i.published_at is None or i.published_at.tzinfo for i in result.items)


@pytest.mark.parametrize(
    ("handler", "error", "status"),
    [
        (RssHandler(status=429), ProviderRateLimitedError, "error"),
        (RssHandler(status=403), ProviderPolicyBlockedError, "policy_blocked"),
        (RssHandler(status=503), ProviderUnavailableError, "error"),
        (RssHandler(exc=httpx2.ReadTimeout("slow")), ProviderTimeoutError, "error"),
        (RssHandler(exc=httpx2.ConnectError("down")), ProviderUnavailableError, "error"),
        (RssHandler(body=b"<not-xml"), ProviderMalformedResponseError, "error"),
        (RssHandler(body=b"<rss></rss>"), ProviderMalformedResponseError, "error"),
    ],
)
async def test_rss_errors_are_typed(make_settings, handler, error, status) -> None:
    ar, _, _ = adapter(make_settings, rss=handler, agent_reach_channels="news_rss")
    with pytest.raises(error) as exc_info:  # every query failed -> run fails with that type
        await ar.research(request(max_queries=2))
    queries = exc_info.value.details["queries"]
    assert len(queries) == 2 and all(q["status"] == status for q in queries)


async def test_xml_bombs_are_refused(make_settings) -> None:
    bomb = b"""<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">
    <!ENTITY lol2 "&lol;&lol;&lol;&lol;">]>
    <rss><channel><item><title>&lol2;</title></item></channel></rss>"""
    ar, _, _ = adapter(make_settings, rss=RssHandler(body=bomb), agent_reach_channels="news_rss")
    with pytest.raises(ProviderMalformedResponseError):
        await ar.research(request(max_queries=1))


async def test_empty_feed_is_empty_not_error(make_settings) -> None:
    empty = b"<rss><channel><title>x</title></channel></rss>"
    ar, _, _ = adapter(make_settings, rss=RssHandler(body=empty), agent_reach_channels="news_rss")
    result = await ar.research(request(max_queries=2))
    assert result.items == []
    assert [q.status for q in result.queries] == ["empty", "empty"]


# ================================================================== youtube (yt-dlp)
async def test_youtube_search_args_and_parsing(make_settings) -> None:
    ar, _, runner = adapter(make_settings, agent_reach_channels="youtube")
    result = await ar.research(request(max_queries=1, max_results=5))
    args = runner.calls[0]
    assert args[0] == "yt-dlp"
    for flag in (
        "--flat-playlist",
        "--dump-json",
        "--no-cookies",
        "--no-cookies-from-browser",
        "--ignore-config",
    ):
        assert flag in args
    assert args[-1] == "ytsearch5:Drivn leasing"
    (item,) = result.items
    assert item.platform == "youtube:Founder Thesis"
    assert item.channel == "youtube"
    assert item.published_at == datetime.fromtimestamp(1790000000, tz=UTC)
    assert result.items_processed == 2 and result.items_dropped == 1


@pytest.mark.parametrize(
    ("runner", "error"),
    [
        (
            FakeRunner(code=1, err="ERROR: Sign in to confirm you're not a bot"),
            ProviderPolicyBlockedError,
        ),
        (FakeRunner(code=1, err="HTTP Error 429: Too Many Requests"), ProviderRateLimitedError),
        (FakeRunner(code=2, err="boom"), ProviderUnavailableError),
        (FakeRunner(exc=FileNotFoundError()), ProviderChannelUnsupportedError),
        (FakeRunner(exc=TimeoutError()), ProviderTimeoutError),
        (FakeRunner(out="{not json"), ProviderMalformedResponseError),
    ],
)
async def test_youtube_errors_are_typed(make_settings, runner, error) -> None:
    ar, _, _ = adapter(make_settings, runner=runner, agent_reach_channels="youtube")
    with pytest.raises(error):
        await ar.research(request(max_queries=1))


async def test_partial_failure_keeps_other_channel_results(make_settings) -> None:
    ar, _, _ = adapter(make_settings, runner=FakeRunner(exc=TimeoutError()))
    result = await ar.research(request(max_queries=2))
    statuses = {q.channel: q.status for q in result.queries}
    assert statuses == {"news_rss": "ok", "youtube": "error"}
    assert result.items  # news signals survived the YouTube timeout


# ================================================================== status / guard
def test_status_reports_channels(make_settings) -> None:
    ar, _, _ = adapter(make_settings)
    status = ar.status()
    assert status.operational and status.paid is False
    assert status.detail == "channels: news_rss, youtube"


def test_youtube_disabled_when_ytdlp_missing(make_settings) -> None:
    s = make_settings(provider_mode="live", agent_reach_ytdlp_path="definitely-not-installed-xyz")
    ar = AgentReachAdapter(s)  # real runner -> PATH lookup
    assert ar.available_channels() == [ResearchChannel.NEWS_RSS]
    assert "yt-dlp not found" in ar.disabled_channels()["youtube"]


async def test_no_channels_selected_refuses_without_network(
    make_settings, network_attempts
) -> None:
    ar = AgentReachAdapter(make_settings(provider_mode="live", agent_reach_channels=""))
    assert ar.status().operational is False
    with pytest.raises(ProviderChannelUnsupportedError):
        await ar.research(request())
    assert network_attempts == []
