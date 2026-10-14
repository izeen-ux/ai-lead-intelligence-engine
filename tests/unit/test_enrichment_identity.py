"""Phase 6 identity resolution: attributable people, role priority, ambiguity, matching."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.domain.enrichment.config import EnrichmentConfig
from app.domain.enrichment.identity import (
    IdentityResolution,
    PageText,
    PersonCandidate,
    PersonIdentity,
    cache_key,
    candidates_from_search,
    email_matches_domain,
    extract_people,
    merge_candidates,
    name_matches,
    normalize_name,
    person_key,
    resolve_identities,
    role_rank,
)
from app.providers.contracts import DiscoveredPerson

CID = uuid.UUID(int=7)
TEAM = """
## Team
### Manav Bansal-CEO
Manav Bansal is the CEO and Co-Founder of Drivn, India's e-mobility platform.
Alpna Jain is the Co-Founder and Chief Business Officer of Drivn, an asset-led platform.
### Madhujeet Chimni- CoFounder
Madhujeet Chimni, based in Singapore, is currently a Co-Founder and Director at Moki and Co.
Independent Director
### Sanjiv Chadha - Independent Director
Priya Nair is the Head of Marketing at Drivn.
Founder & CEO
Rahul Mehta is a Senior Engineer at Drivn.
"""


def page(md: str = TEAM, url: str = "https://www.drivn.co.in/team") -> PageText:
    return PageText(url=url, markdown=md, page_id=uuid.uuid4())


def resolve(cands: list[PersonCandidate], **cfg: Any) -> IdentityResolution:
    return resolve_identities(
        cands, company_id=CID, company_name="Drivn", domain="drivn.co.in",
        config=EnrichmentConfig(**cfg),
    )  # fmt: skip


def test_extracts_people_from_own_pages_with_provenance() -> None:
    people = {c.full_name: c for c in extract_people([page()], "Drivn")}
    assert set(people) == {
        "Manav Bansal", "Alpna Jain", "Madhujeet Chimni", "Sanjiv Chadha",
    }  # fmt: skip
    manav = people["Manav Bansal"]
    assert "CEO and Co-Founder" in (manav.title or "")
    assert manav.sources == ["https://www.drivn.co.in/team"]
    assert len(manav.source_ids) == 1
    assert "Rahul Mehta" not in people  # no senior role word -> not a candidate
    assert "Priya Nair" not in people  # "<title> of Marketing at Drivn": org must follow directly


def test_bio_about_another_company_is_not_attributed() -> None:
    people = {c.full_name: c for c in extract_people([page()], "Drivn")}
    # "Co-Founder and Director at Moki" must not attach to Drivn; only the Drivn heading does
    assert people["Madhujeet Chimni"].title == "CoFounder"


def test_newlines_never_join_a_title_and_a_name() -> None:
    names = {
        c.full_name
        for c in extract_people([page("Founder & CEO\nRiya Kapoor is the CEO of Drivn.")], "Drivn")
    }
    assert names == {"Riya Kapoor"}


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Founder & CEO", (0, "founder", 2)),
        ("CoFounder", (0, "founder", 2)),
        ("Chief Executive Officer", (2, "ceo", 2)),
        ("CTO", (5, "cto", 1)),
        ("Independent Director", None),
        ("Advisor to the CEO", None),
        ("Former CEO", None),
        ("Head of Marketing", None),
        (None, None),
    ],
)
def test_role_rank(title, expected) -> None:
    assert role_rank(title, EnrichmentConfig().role_priority) == expected


def test_role_priority_ceo_founder_beats_plain_cofounder_and_is_configurable() -> None:
    cands = extract_people([page()], "Drivn")
    res = resolve(cands)
    assert [p.full_name for p in res.selected] == ["Manav Bansal"]
    assert any("max persons" in why for _, why in res.skipped)
    cto_first = resolve(
        [PersonCandidate("Kiran Rao", "CTO", ["u"]), PersonCandidate("Asha Iyer", "COO", ["u"])],
        role_priority=("cto", "coo"),
    )
    assert cto_first.selected[0].full_name == "Kiran Rao"


def test_multiple_founders_up_to_the_configured_limit() -> None:
    res = resolve(extract_people([page()], "Drivn"), max_persons_per_company=3)
    assert [p.full_name for p in res.selected] == ["Manav Bansal", "Alpna Jain", "Madhujeet Chimni"]
    assert {n for n, _ in res.skipped} == {"Sanjiv Chadha"}  # independent director


def test_ambiguous_senior_people_are_never_enriched() -> None:
    res = resolve(
        [
            PersonCandidate("Manav Bansal", "CEO", ["u"]),
            PersonCandidate("Manav Bhatia", "Co-Founder", ["u"]),
        ]
    )
    assert res.status == "ambiguous"
    assert res.selected == []
    assert res.ambiguous == ["Manav Bansal", "Manav Bhatia"]


@pytest.mark.parametrize("name", ["Manav", "M Bansal", "Manav B"])
def test_incomplete_names_are_skipped(name) -> None:
    res = resolve([PersonCandidate(name, "CEO", ["u"])])
    assert res.status == "no_person"
    assert "incomplete name" in res.skipped[0][1]


def test_no_person() -> None:
    assert resolve([]).status == "no_person"
    assert resolve(extract_people([page("We build EVs.")], "Drivn")).status == "no_person"


def test_search_candidates_merge_with_page_candidates() -> None:
    found = candidates_from_search(
        [
            DiscoveredPerson(
                first_name="Manav",
                last_name="Bansal",
                position="CEO",
                email="m@drivn.co.in",
                confidence=0.9,
            ),
            DiscoveredPerson(first_name="Anon", last_name=None, position="CEO"),
        ],
        "drivn.co.in",
    )
    assert [c.full_name for c in found] == ["Manav Bansal"]
    merged = merge_candidates(extract_people([page()], "Drivn"), found)
    manav = next(c for c in merged if c.full_name == "Manav Bansal")
    assert manav.sources == ["https://www.drivn.co.in/team", "hunter_domain_search:drivn.co.in"]
    assert manav.email_hint == "m@drivn.co.in"


def test_keys_are_normalized() -> None:
    assert normalize_name("Mr.  Manav   BANSAL") == "manav bansal"
    assert person_key("Drivn.co.in", "Mr. Manav Bansal") == "drivn.co.in|manav bansal"
    assert cache_key("drivn.co.in", "Manav Bansal") == cache_key("DRIVN.CO.IN", "manav  bansal")
    assert len(cache_key("drivn.co.in", "Manav Bansal")) == 40


IDENTITY = PersonIdentity(
    company_id=CID, company_name="Drivn", domain="drivn.co.in", full_name="Manav Bansal",
    first_name="Manav", last_name="Bansal", title="CEO", role="ceo", role_rank=2,
    sources=("u",), source_ids=(),
)  # fmt: skip


@pytest.mark.parametrize(
    ("first", "last", "full", "ok"),
    [
        ("Manav", "Bansal", None, True),
        ("M", "Bansal", None, True),
        (None, None, None, True),  # provider echoed no name: not evidence of a mismatch
        ("Someone", "Else", "Someone Else", False),
        ("Manav", "Gupta", None, False),
        ("Rohan", "Bansal", None, False),
    ],
)
def test_person_matching(first, last, full, ok) -> None:
    assert name_matches(IDENTITY, first, last, full) is ok


@pytest.mark.parametrize(
    ("email", "ok"),
    [
        ("manav@drivn.co.in", True),
        ("manav@mail.drivn.co.in", True),
        ("manav@otherdrivn.co.in", False),
        ("manav@gmail.com", False),
        (None, False),
        ("broken", False),
    ],
)
def test_email_domain_matching(email, ok) -> None:
    assert email_matches_domain(email, "drivn.co.in") is ok
