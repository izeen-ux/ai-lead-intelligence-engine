from __future__ import annotations

import pytest

from app.domain.identity import (
    canonicalize_url,
    company_dedupe_key,
    normalize_company_name,
    url_hash,
)
from app.utils.funding_headlines import (
    extract_company_from_headline,
    extract_funding_mention,
    extract_funding_round,
)


# ---------------------------------------------------------------- company identity
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Acme Robotics", "acme robotics"),
        ("ACME Robotics Pvt. Ltd.", "acme robotics"),
        ("Acme Robotics Private Limited", "acme robotics"),
        ("  acme   robotics, inc ", "acme robotics"),
        ("Tata & Sons", "tata and sons"),
        ("\uff26\uff55\uff4c\uff4c\uff57\uff49\uff44\uff54\uff48 Co", "fullwidth"),
    ],
)
def test_normalize_company_name(raw: str, expected: str) -> None:
    assert normalize_company_name(raw) == expected


def test_normalize_company_name_rejects_empty() -> None:
    with pytest.raises(ValueError, match="normalizes to nothing"):
        normalize_company_name(" ,.- ")


def test_dedupe_key_prefers_domain_and_is_stable() -> None:
    assert company_dedupe_key("Acme", "https://www.Acme.io/about") == "domain:acme.io"
    assert company_dedupe_key("ACME Robotics Pvt Ltd") == company_dedupe_key("acme robotics")
    assert company_dedupe_key("Acme") != company_dedupe_key("Acme", "acme.io")


# ---------------------------------------------------------------- url identity
@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("https://News.Example/a/?utm_source=x#top", "https://news.example/a"),
        ("https://news.example:443/a?b=2&a=1", "https://news.example/a?a=1&b=2"),
        ("https://news.example/a?gclid=1&fbclid=2", "https://news.example/a"),
    ],
)
def test_equivalent_urls_share_hash(a: str, b: str) -> None:
    assert canonicalize_url(a) == canonicalize_url(b)
    assert url_hash(a) == url_hash(b)


def test_distinct_urls_differ() -> None:
    assert url_hash("https://news.example/a") != url_hash("https://news.example/b")
    assert canonicalize_url("http://x.example:8080/") == "http://x.example:8080/"


# ---------------------------------------------------------------- headlines
@pytest.mark.parametrize(
    ("title", "company"),
    [
        ("Acme Robotics raises Rs 3 Cr in seed round led by Alpha", "Acme Robotics"),
        ("Exclusive: Fintech startup Globex Pay bags $1.2 Mn in pre-Series A", "Globex Pay"),
        ("Bengaluru-based Initech secures ₹5 crore funding", "Initech"),
        ("D2C brand Snitch raises $40 Mn in Series B", "Snitch"),
        ("[Funding alert] Zeta mops up INR 2 Cr from angels", "Zeta"),
        ("Acme \u2013 raises seed funding", "Acme"),
        ("\U0001f916 Dextr AI Raises $6.7M Seed Round", "Dextr AI"),  # seen live 2026-09-28
        ("Drivn Secures Rs 45 Cr Seed Funding", "Drivn"),
        ("Top 10 Indian startups that raised funding this week", None),
        ("How to raise a seed round in India", None),
        ("Initech gets new CEO", None),  # no funding context
        ("startup raises money", None),  # no proper name
        ("A very long descriptive phrase that is not a name raises Rs 2 Cr", None),
        ("2 startups raise Rs 10 Cr", None),
    ],
)
def test_extract_company_from_headline(title: str, company: str | None) -> None:
    assert extract_company_from_headline(title) == company


@pytest.mark.parametrize(
    ("text", "mention"),
    [
        ("Acme raises Rs 3 Cr in seed", "Rs 3 Cr"),
        ("bags $1.2 Mn", "$1.2 Mn"),
        ("secures ₹5 crore", "₹5 crore"),
        ("raised 50 lakh from angels", "50 lakh"),
        ("no amount here", None),
    ],
)
def test_extract_funding_mention(text: str, mention: str | None) -> None:
    assert extract_funding_mention(text) == mention


def test_extract_funding_round() -> None:
    assert extract_funding_round("in a pre-Series A round") == "pre-series a"
    assert extract_funding_round("seed round") == "seed"
    assert extract_funding_round("growth capital") is None
