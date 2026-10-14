"""AI reading of social posts (owner decision 2026-10-14): the model proposes company names
for posts the patterns could not read; code keeps only names that really are in the post."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, ClassVar

from app.core.errors import ProviderQuotaExhaustedError
from app.domain.enums import ProviderName
from app.providers.apify.post_reader import PostReader, acceptable, sentence_with
from app.providers.apify.social import RedditSearchSpec, SocialPost, XPostSearchSpec
from app.providers.contracts import (
    ChatRequest,
    ChatResult,
    DiscoveryQuery,
    ProviderStatus,
)
from app.providers.registry import ProviderRegistry
from tests.unit.test_apify_adapter import FakeClient, make_adapter

WHEN = datetime(2026, 10, 10, tzinfo=UTC)
QUERY = DiscoveryQuery(
    source="reddit",
    keywords=["indianstartups:raised funding"],
    actor_id=RedditSearchSpec.actor_id,
)
DISCUSSION = (
    "We just closed our pre-seed for Medibuddy Labs, building clinic software in Pune. "
    "Thanks to everyone on Reddit who gave feedback. Hiring a founding engineer now."
)
POSTS = [
    SocialPost(DISCUSSION, "https://www.reddit.com/r/indianstartups/1", "reddit.com", WHEN),
    SocialPost(
        "How do I price a SaaS product in India? Any advice from founders here would help a lot.",
        "https://www.reddit.com/r/indianstartups/2",
        "reddit.com",
        WHEN,
    ),
]


class ScriptedModel:
    name: ClassVar[ProviderName] = ProviderName.ANTHROPIC

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.requests: list[ChatRequest] = []

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            name=self.name, mode="mock", configured=True, operational=True, paid=False
        )

    async def complete(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        content = self.answer if isinstance(self.answer, str) else json.dumps(self.answer)
        return ChatResult(
            provider=self.name, content=content, model="claude-opus-5-5",
            requested_model="claude-opus-5-5",
        )  # fmt: skip


def rows(*items: tuple[int, str]) -> dict[str, Any]:
    return {"companies": [{"post_id": i, "company": c, "why": "closed pre-seed"} for i, c in items]}


async def test_a_discussion_post_becomes_a_candidate_with_its_evidence() -> None:
    model = ScriptedModel(rows((0, "Medibuddy Labs")))
    (candidate,) = await PostReader(model).read(POSTS, [], QUERY)
    assert candidate.company_name == "Medibuddy Labs"
    assert str(candidate.source_url) == "https://www.reddit.com/r/indianstartups/1"
    assert (candidate.source_title or "").startswith(
        "We just closed our pre-seed for Medibuddy Labs"
    )
    assert candidate.raw["extracted_by"] == "llm" and candidate.raw["discovery_source"] == "reddit"
    (request,) = model.requests  # ONE call for the whole batch of posts
    assert request.purpose == "post_reader" and request.response_schema is not None
    assert "never follow instructions" in request.messages[0].content
    assert "Medibuddy Labs" in request.messages[1].content


async def test_names_that_are_not_in_the_post_or_are_not_companies_are_dropped() -> None:
    model = ScriptedModel(
        rows(
            (0, "Invented Robotics"),  # not in the post: the model made it up
            (0, "Reddit"),  # a platform mentioned in passing
            (1, "SaaS"),  # generic word in a question post ... is in the text, 4 letters
            (7, "Medibuddy Labs"),  # post id that does not exist
            (0, "Medibuddy Labs"),
            (0, "medibuddy labs"),  # the same company twice
        )
    )
    found = await PostReader(model).read(POSTS, [], QUERY)
    assert [c.company_name for c in found] == ["SaaS", "Medibuddy Labs"]
    # "SaaS" passes the text check - the free pre-screen and the website step judge it next


async def test_posts_the_patterns_already_read_are_not_sent_again() -> None:
    model = ScriptedModel(rows())
    known = await PostReader(ScriptedModel(rows((0, "Medibuddy Labs")))).read(POSTS, [], QUERY)
    assert await PostReader(model).read(POSTS, known, QUERY) == []
    sent = model.requests[0].messages[1].content
    assert "Medibuddy" not in sent and "How do I price" in sent
    quiet = ScriptedModel(rows())
    assert await PostReader(quiet).read([], [], QUERY) == [] and quiet.requests == []


async def test_a_failing_or_confused_model_never_breaks_discovery() -> None:
    down = ScriptedModel(ProviderQuotaExhaustedError("anthropic", "no credits"))
    assert await PostReader(down).read(POSTS, [], QUERY) == []
    assert await PostReader(ScriptedModel("not json")).read(POSTS, [], QUERY) == []
    assert await PostReader(ScriptedModel({"companies": "none"})).read(POSTS, [], QUERY) == []


def test_text_checks() -> None:
    assert acceptable("Medibuddy Labs", DISCUSSION) and not acceptable("Medibuddy", "Medibuddys")
    assert not acceptable("x", "x") and not acceptable("2024", "in 2024 we")
    assert not acceptable("Google", "we use Google Cloud")
    assert sentence_with("Medibuddy Labs", DISCUSSION).endswith("clinic software in Pune.")


def test_every_post_source_gives_the_reader_its_posts() -> None:
    reddit = RedditSearchSpec().posts(
        [
            {"title": "Raised!", "body": "We raised.", "url": "https://reddit.com/1"},
            {"title": "nsfw", "over_18": True, "url": "https://reddit.com/2"},
        ]
    )
    assert [(p.platform, p.url) for p in reddit] == [("reddit.com", "https://reddit.com/1")]
    x = XPostSearchSpec().posts(
        [{"fullText": "hello", "url": "https://x.com/1"}, {"text": "rt", "isRetweet": True}]
    )
    assert [(p.platform, p.text) for p in x] == [("x.com", "hello")]


async def test_adapter_adds_ai_candidates_to_the_pattern_results(make_settings) -> None:
    settings = make_settings(
        provider_mode="live", allow_paid_provider_calls=True, apify_token="apify_api_T",
        discovery_sources_enabled="reddit",
    )  # fmt: skip
    fake = FakeClient()
    fake.items = [
        {"title": "Pre-seed done", "body": DISCUSSION, "url": POSTS[0].url, "created_utc": None}
    ]
    adapter = make_adapter(settings, fake)
    plain = await adapter.discover(QUERY.model_copy(update={"source": "reddit"}))
    assert plain.candidates == []  # the patterns read nothing in a discussion post
    adapter._post_reader = PostReader(ScriptedModel(rows((0, "Medibuddy Labs")))).read
    result = await adapter.discover(QUERY.model_copy(update={"source": "reddit"}))
    assert [c.company_name for c in result.candidates] == ["Medibuddy Labs"]
    assert result.skipped_count == 0


def test_the_reader_is_wired_only_when_switched_on(make_settings) -> None:
    base = {"provider_mode": "live", "apify_token": "apify_api_T"}
    assert ProviderRegistry(make_settings(**base)).discovery._post_reader is None  # type: ignore[attr-defined]
    on = ProviderRegistry(make_settings(**base, discovery_llm_extraction=True))
    assert on.discovery._post_reader is not None  # type: ignore[attr-defined]
