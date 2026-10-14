"""Founder lookup in public web-search results when the company's own pages name nobody:
conservative name / role / company matching, budget, provenance, and no page fetches."""

from __future__ import annotations

from typing import Any

import pytest

from app.core.errors import ProviderUnavailableError
from app.domain.enrichment.identity import candidates_from_web_search
from app.domain.enums import ContactStatus, ProviderName
from app.providers.base import MockProviderBase
from app.providers.contracts import WebSearchHit, WebSearchQuery, WebSearchResult
from tests.enrichment_factories import company, world

LINKEDIN = "https://www.linkedin.com/in/asha-rao-123"
HITS = [
    (LINKEDIN, "Asha Rao - Founder & CEO - Acme Robotics | LinkedIn . Bengaluru, India"),
    ("https://news.example/a", "How Acme Robotics founder Asha Rao built a warehouse robot"),
    ("https://news.example/b", "Vikram Nair, CEO of Globex Analytics, on pricing . Acme Robotics"),
    ("https://news.example/c", "Top 10 founders to watch this year"),
]


class SearchStub(MockProviderBase):
    name = ProviderName.FIRECRAWL

    def __init__(self, hits: list[tuple[str, str]], fail: bool = False) -> None:
        super().__init__(None)
        self.hits, self.fail = hits, fail
        self.queries: list[WebSearchQuery] = []

    async def search_web(self, query: WebSearchQuery) -> WebSearchResult:
        self.queries.append(query)
        if self.fail:
            raise ProviderUnavailableError("firecrawl", "search down")
        hits = [
            WebSearchHit(url=u, title=t.split(" . ")[0], description=t, position=i)
            for i, (u, t) in enumerate(self.hits, start=1)
        ]
        return WebSearchResult(provider=self.name, query=query.query, hits=hits, credits_used=2)


# ------------------------------------------------------------------ parsing
def test_founder_is_read_from_result_titles_and_snippets() -> None:
    [asha] = candidates_from_web_search(HITS, "Acme Robotics")
    assert asha.full_name == "Asha Rao" and "Founder" in (asha.title or "")
    assert asha.sources == [LINKEDIN, "https://news.example/a"]
    assert asha.linkedin_url == LINKEDIN  # result URL only - the page is never requested


@pytest.mark.parametrize(
    "text",
    [
        "Vikram Nair, CEO of Globex Analytics, speaks at the Acme Robotics summit",  # other firm
        "Asha Rao joins Acme Robotics as marketing intern",  # not a decision-maker role
        "Acme Robotics raises seed round to build robots",  # nobody named
        "Founder stories: how to hire your first engineer",
    ],
)
def test_other_companies_and_non_roles_yield_nobody(text: str) -> None:
    assert candidates_from_web_search([("https://news.example/x", text)], "Acme Robotics") == []


def test_non_profile_urls_never_become_a_linkedin_link() -> None:
    hits = [
        ("https://www.linkedin.com/company/acme", "Asha Rao - Founder - Acme Robotics | LinkedIn")
    ]
    [asha] = candidates_from_web_search(hits, "Acme Robotics")
    assert asha.linkedin_url is None


# ------------------------------------------------------------------ service
async def run(make_settings, search: Any, **settings: Any):
    w = await world(make_settings, company(people=""), **settings)  # website names nobody
    return w, await w.service(web_search=search).run(w.ids)


async def test_no_person_on_site_falls_back_to_web_search(make_settings) -> None:
    search = SearchStub(HITS)
    w, (_, [contact]) = await run(make_settings, search)
    assert contact.status is ContactStatus.VERIFIED and contact.full_name == "Asha Rao"
    assert contact.email == "asha.rao@acmerobotics.in"  # found + verified through Hunter
    assert LINKEDIN in contact.provenance["identity"]["sources"]
    [query] = search.queries
    assert '"Acme Robotics"' in query.query and "acmerobotics.in" in query.query
    assert [r.person_name for r in w.kit.requests] == ["Asha Rao"]


async def test_without_a_search_provider_or_when_disabled_nothing_changes(make_settings) -> None:
    w = await world(make_settings, company(people=""))
    _, [none] = await w.service().run(w.ids)
    assert none.status is ContactStatus.NO_PERSON and w.kit.requests == []
    search = SearchStub(HITS)
    _, (_, [off]) = await run(make_settings, search, enrichment_web_search=False)
    assert off.status is ContactStatus.NO_PERSON and search.queries == []


async def test_search_is_skipped_when_the_site_already_names_the_founder(make_settings) -> None:
    search = SearchStub(HITS)
    w = await world(make_settings, company(people="## Team\n### Meera Shah - Founder & CEO"))
    _, [contact] = await w.service(web_search=search).run(w.ids)
    assert contact.full_name == "Meera Shah" and search.queries == []


async def test_search_budget_and_failures_are_harmless(make_settings) -> None:
    search = SearchStub(HITS)
    _, (_, [capped]) = await run(make_settings, search, enrichment_max_web_searches_per_run=0)
    assert capped.status is ContactStatus.NO_PERSON and search.queries == []
    broken = SearchStub(HITS, fail=True)
    _, (_, [failed]) = await run(make_settings, broken)
    assert failed.status is ContactStatus.NO_PERSON and len(broken.queries) == 1


async def test_nobody_is_invented_when_results_name_no_founder(make_settings) -> None:
    search = SearchStub([("https://news.example/c", "Top 10 founders to watch this year")])
    w, (_, [contact]) = await run(make_settings, search)
    assert contact.status is ContactStatus.NO_PERSON and contact.full_name is None
    assert w.kit.requests == []  # no paid lookup without a named person


def test_names_do_not_swallow_the_following_word() -> None:
    """Regression (live run): "Naveen Sowriraj is co-founder of Creedom" gave "... Sowriraj is"."""
    hits = [("https://news.example/n", "Naveen Sowriraj is co-founder of Creedom, an AI startup")]
    [person] = candidates_from_web_search(hits, "Creedom")
    assert person.full_name == "Naveen Sowriraj" and person.title == "co-founder"
    lower = [("https://news.example/l", "naveen sowriraj is the Founder of Creedom")]
    assert candidates_from_web_search(lower, "Creedom") == []  # not a properly written name
