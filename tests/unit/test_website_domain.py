"""Pure Phase 3 logic: URL policy, page selection/budget, sufficiency, identity, resolution."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.domain.domain_resolution import (
    build_search_query,
    decide,
    score_candidates,
    site_domain,
)
from app.domain.enums import DomainResolutionStatus, PageType
from app.domain.website import (
    assess_sufficiency,
    build_site_facts,
    classify_page,
    crawlable_domain,
    extract_social_links,
    is_non_official_host,
    is_social_url,
    mentions_company,
    select_pages,
)


@dataclass
class Page:
    page_type: PageType
    markdown: str
    title: str | None = None
    description: str | None = None


@dataclass
class Hit:
    url: str
    title: str | None
    position: int
    description: str | None = None


# ---------------------------------------------------------------- URL policy
@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/company/drivn",
        "https://in.linkedin.com/in/founder",
        "https://instagram.com/p/abc",
        "https://x.com/acme",
        "https://twitter.com/acme",
        "https://m.facebook.com/acme",
        "https://youtu.be/xyz",
    ],
)
def test_social_urls_are_detected(url: str) -> None:
    assert is_social_url(url)


def test_non_official_hosts() -> None:
    assert is_non_official_host("www.crunchbase.com")
    assert is_non_official_host("inc42.com")
    assert is_non_official_host("economictimes.indiatimes.com")
    assert not is_non_official_host("drivn.in")


@pytest.mark.parametrize(
    "bad", ["intranet.local", "db.internal", "x.test", "router.lan", "linkedin.com"]
)
def test_crawlable_domain_refuses_internal_and_non_official(bad: str) -> None:
    with pytest.raises(ValueError, match="not allowed"):
        crawlable_domain(bad)


def test_crawlable_domain_canonicalizes() -> None:
    assert crawlable_domain("https://WWW.Drivn.in/about") == "drivn.in"


# ---------------------------------------------------------------- page classification/selection
@pytest.mark.parametrize(
    ("url", "page_type"),
    [
        ("https://acme.io/", PageType.HOME),
        ("https://acme.io", PageType.HOME),
        ("https://acme.io/about-us", PageType.ABOUT),
        ("https://acme.io/company/team", PageType.ABOUT),
        ("https://acme.io/solutions/fleet", PageType.PRODUCTS),
        ("https://acme.io/careers", PageType.CAREERS),
        ("https://acme.io/pricing", PageType.PRICING),
        ("https://acme.io/blog/launch", PageType.BLOG),
        ("https://acme.io/contact-us", PageType.CONTACT),
        ("https://acme.io/faq", PageType.OTHER),
    ],
)
def test_classify_page(url: str, page_type: PageType) -> None:
    assert classify_page(url) == page_type


SITE_URLS = [
    "https://acme.io/blog/post-1",
    "https://acme.io/blog",
    "https://acme.io/about",
    "https://acme.io/en/about",
    "https://acme.io/products",
    "https://acme.io/careers",
    "https://acme.io/pricing",
    "https://acme.io/contact",
    "https://acme.io/faq",
    "https://acme.io/privacy-policy",  # utility page - skipped
    "https://acme.io/brochure.pdf",  # file - skipped
    "https://www.linkedin.com/company/acme",  # social - never crawled
    "https://other.example/about",  # off-site
    "mailto:hi@acme.io",
    "https://docs.acme.io/guide",  # own subdomain allowed
]


def test_select_pages_priority_and_policy() -> None:
    plan = select_pages("acme.io", SITE_URLS, budget=8)
    assert [p.page_type for p in plan] == [
        PageType.HOME,
        PageType.ABOUT,
        PageType.PRODUCTS,
        PageType.CAREERS,
        PageType.PRICING,
        PageType.BLOG,
        PageType.CONTACT,
        PageType.OTHER,
    ]
    urls = [p.url for p in plan]
    assert urls[0] == "https://acme.io/"
    assert "https://acme.io/about" in urls  # shortest path wins over /en/about
    assert "https://acme.io/blog" in urls
    assert not any("linkedin" in u or "other.example" in u or u.endswith(".pdf") for u in urls)
    assert not any("privacy" in u for u in urls)


@pytest.mark.parametrize("budget", [0, 1, 3, 8, 20])
def test_select_pages_never_exceeds_budget(budget: int) -> None:
    plan = select_pages("acme.io", SITE_URLS, budget)
    assert len(plan) <= max(budget, 0)
    if budget >= 1:
        assert plan[0].page_type is PageType.HOME


def test_select_pages_with_nothing_discovered_still_plans_home() -> None:
    assert [p.url for p in select_pages("acme.io", [], 8)] == ["https://acme.io/"]


def test_extract_social_links_is_metadata_only() -> None:
    links = extract_social_links(
        ["https://www.linkedin.com/company/acme", "https://acme.io/x", "https://x.com/acme"]
    )
    assert links == {
        "linkedin.com": "https://www.linkedin.com/company/acme",
        "x.com": "https://x.com/acme",
    }


# ---------------------------------------------------------------- sufficiency / facts / identity
def test_sufficiency_rules() -> None:
    assert assess_sufficiency([]) == (False, "no pages extracted")
    thin = [Page(PageType.HOME, "hi")]
    assert assess_sufficiency(thin)[0] is False
    ok = [Page(PageType.HOME, "x" * 300), Page(PageType.ABOUT, "y" * 300)]
    assert assess_sufficiency(ok) == (True, None)
    no_home = [Page(PageType.ABOUT, "y" * 900)]
    assert assess_sufficiency(no_home)[0] is False


def test_site_facts_are_evidence_backed() -> None:
    facts = build_site_facts(
        [
            Page(
                PageType.HOME, "We build AI agents and automation for fleets.", "Drivn", "EV fleet"
            ),
            Page(PageType.CAREERS, "Join us"),
            Page(PageType.PRICING, "Plans"),
        ]
    )
    assert facts["home_title"] == "Drivn"
    assert facts["page_types_found"] == ["home", "careers", "pricing"]
    assert facts["has_careers_page"] and facts["has_pricing_page"]
    assert not facts["has_blog_page"]
    assert facts["ai_term_mentions_by_page"] == {"home": 2}


@pytest.mark.parametrize(
    ("title", "text", "name", "expected"),
    [
        ("Dextr AI - Home", "", "dextr ai", True),
        (None, "Welcome to DextrAI, the agent platform", "dextr ai", True),
        ("Drivn | Electric mobility", "", "drivn", True),
        ("Unrelated Corp", "we sell shoes", "drivn", False),
        ("x", "", "ab", False),  # too short to verify
    ],
)
def test_mentions_company(title, text, name, expected) -> None:
    pages = [Page(PageType.HOME, text or "content", title)]
    assert mentions_company(pages, name) is expected


def test_mentions_company_requires_homepage() -> None:
    assert mentions_company([Page(PageType.ABOUT, "Drivn")], "drivn") is False


# ---------------------------------------------------------------- domain resolution
def test_site_domain_drops_app_and_locale_subdomains() -> None:
    assert site_domain("https://www.drivn.in/") == "drivn.in"
    assert site_domain("https://app.dextr.ai/login") == "dextr.ai"
    assert site_domain("https://en.acme.co.in/") == "acme.co.in"
    assert site_domain("https://blog.example.com") == "example.com"
    assert site_domain("https://intranet.local/") is None
    assert site_domain("not a url") is None


def test_build_search_query() -> None:
    assert build_search_query("Drivn", "IN") == '"Drivn" official website India'
    assert build_search_query("Drivn") == '"Drivn" official website'


def test_official_site_is_resolved_and_exclusions_explained() -> None:
    hits = [
        Hit("https://www.linkedin.com/company/drivn", "Drivn | LinkedIn", 1),
        Hit("https://inc42.com/buzz/drivn-raises", "Drivn raises Rs 45 Cr", 2),
        Hit("https://drivn.in/", "Drivn - Electric mobility for fleets", 3),
        Hit("https://news.example/drivn", "Drivn funding", 4),
    ]
    decision = decide("drivn", hits, min_confidence=0.7, excluded_hosts={"news.example"})
    assert decision.status is DomainResolutionStatus.RESOLVED
    assert decision.domain == "drivn.in"
    assert decision.confidence >= 0.7
    reasons = {c.domain: c.excluded_reason for c in decision.candidates}
    assert reasons["linkedin.com"] == "social platform (metadata only)"
    assert reasons["inc42.com"] == "directory/news/app-store host"
    assert reasons["news.example"] == "publisher of the company's discovery source"
    assert decision.social_links == {"linkedin.com": "https://www.linkedin.com/company/drivn"}


def test_title_mention_alone_never_resolves() -> None:
    hits = [Hit("https://someblog.com/post", "Drivn raises funding - Drivn story", 1, "Drivn")]
    decision = decide("drivn", hits, min_confidence=0.7)
    assert decision.status is DomainResolutionStatus.REVIEW
    assert decision.confidence < 0.7


def test_ambiguous_candidates_go_to_review() -> None:
    hits = [
        Hit("https://drivn.in/", "Drivn", 1),
        Hit("https://drivn.com/", "Drivn", 2),
    ]
    decision = decide("drivn", hits, min_confidence=0.7)
    assert decision.status is DomainResolutionStatus.REVIEW
    assert "ambiguous" in decision.reason


def test_nothing_plausible_is_not_found() -> None:
    hits = [
        Hit("https://www.linkedin.com/company/x", "X", 1),
        Hit("https://crunchbase.com/x", "X", 2),
    ]
    decision = decide("zeta labs", hits, min_confidence=0.7)
    assert decision.status is DomainResolutionStatus.NOT_FOUND
    assert decision.domain is None


def test_multi_word_names_and_primary_token() -> None:
    hits = [Hit("https://dextr.ai/", "Dextr AI - AI agents for sales", 1)]
    decision = decide("dextr ai", hits, min_confidence=0.7)
    assert decision.status is DomainResolutionStatus.RESOLVED
    assert decision.domain == "dextr.ai"


def test_partial_name_in_domain_scores_lower() -> None:
    scored, _ = score_candidates("drivn", [Hit("https://getdrivn.com/", "Get Drivn", 3)])
    assert scored[0].evidence[0] == "domain contains company name"
    assert scored[0].score < 0.7  # needs more evidence than a partial match
