"""Company profile fields of a lead (location, industry, company LinkedIn, funding round).

Every value is DERIVED from records the pipeline already stored - nothing is fetched and
nothing is guessed. Each field carries where it came from; when the stored evidence does not
support a value the field stays empty (``NOT_FOUND`` in the provenance), which is a legitimate
outcome and not an error.

  location          companies.location / country_code, else the geography hard rule that passed
  industry          companies.industry, else a keyword match on the company's OWN homepage
                    title / description (the matched term is recorded)
  company LinkedIn  a ``linkedin.com/company/...`` link found on the company website (public
                    link metadata only - the LinkedIn page itself is never fetched)
  funding           the attributed funding event of the qualification decision
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from app.utils.funding_headlines import extract_funding_round

NOT_FOUND = "NOT_FOUND"
_COUNTRIES = {"IN": "India"}

# (label, terms). First match wins; terms are matched as whole words, case-insensitive.
_INDUSTRIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Electric mobility",
        ("ev", "e-mobility", "electric vehicle", "electric vehicles", "electric three-wheeler"),
    ),
    ("Fintech", ("fintech", "payments", "lending", "neobank", "insurtech", "invoicing")),
    ("Healthtech", ("healthtech", "healthcare", "telemedicine", "clinic", "diagnostics")),
    ("Agritech", ("agritech", "agri", "farmers", "crop", "farm")),
    ("Edtech", ("edtech", "learning platform", "upskilling", "tutoring")),
    ("Consumer services", ("laundry", "dry cleaning", "home services", "salon")),
    ("Logistics", ("logistics", "freight", "fleet", "supply chain", "last-mile")),
    ("D2C / consumer brand", ("d2c", "direct-to-consumer", "skincare", "apparel", "snacks")),
    ("E-commerce", ("e-commerce", "ecommerce", "marketplace", "quick commerce", "grocery")),
    ("SaaS / software", ("saas", "software", "platform", "api", "automation")),
)
_LINKEDIN_COMPANY = re.compile(
    r"^/(company|school|showcase)/(?P<slug>[A-Za-z0-9\-_%.]+)", re.IGNORECASE
)


@dataclass(frozen=True)
class CompanyProfile:
    location: str = ""
    industry: str = ""
    company_linkedin_url: str = ""
    funding_round_hint: str = ""  # round named by the discovery headline, when stored
    provenance: dict[str, str] = field(default_factory=dict)


def classify_industry(text: str) -> tuple[str, str] | None:
    """(industry label, matched term) from a company's own description, or None."""
    lowered = text.lower()
    for label, terms in _INDUSTRIES:
        for term in terms:
            if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", lowered):
                return label, term
    return None


def company_linkedin(social_links: Mapping[str, Any]) -> str:
    """Canonical company-page URL from website link metadata; '' for personal profiles."""
    for host, url in social_links.items():
        if "linkedin.com" not in str(host).lower() or not isinstance(url, str):
            continue
        parts = urlsplit(url)
        match = _LINKEDIN_COMPANY.match(parts.path)
        if match and (parts.hostname or "").lower().endswith("linkedin.com"):
            return f"https://www.linkedin.com/{match.group(1).lower()}/{match.group('slug')}"
    return ""


def build_profile(
    *,
    location: str | None = None,
    country_code: str | None = None,
    industry: str | None = None,
    description: str | None = None,
    social_links: Mapping[str, Any] | None = None,
    funding_round: str | None = None,
    hard_rules: list[dict[str, Any]] | None = None,
) -> CompanyProfile:
    prov: dict[str, str] = {}
    place = (location or "").strip()
    if place:
        prov["company_location"] = "company record"
    elif country_code and country_code.upper() in _COUNTRIES:
        place, prov["company_location"] = _COUNTRIES[country_code.upper()], "company record"
    else:
        geo = next(
            (r for r in hard_rules or [] if r.get("rule") == "geography"), None
        )  # the ICP geography rule only passes for the ICP country
        if geo is not None and geo.get("outcome") == "pass":
            place = "India"
            prov["company_location"] = f"qualification geography rule: {geo.get('reason', '')}"
    sector = (industry or "").strip()
    if sector:
        prov["industry"] = "company record"
    elif description and (hit := classify_industry(description)) is not None:
        sector = hit[0]
        prov["industry"] = f"keyword '{hit[1]}' on the company's homepage"
    page = company_linkedin(social_links or {})
    if page:
        prov["company_linkedin_url"] = "link on the company website"
    return CompanyProfile(
        location=place,
        industry=sector,
        company_linkedin_url=page,
        funding_round_hint=(funding_round or "").strip(),
        provenance={
            key: prov.get(key, NOT_FOUND)
            for key in ("company_location", "industry", "company_linkedin_url")
        },
    )


def funding_of(events: list[dict[str, Any]], round_hint: str = "") -> dict[str, str]:
    """Amount / round / date of the newest attributed funding event in a decision's evidence
    bundle. Empty strings when the decision has no funding event (e.g. the intent path)."""
    event = next(
        (e for e in events if "funding" in (e.get("types") or []) and e.get("attributed", True)),
        None,
    )
    if event is None:
        return {"amount": "", "round": "", "date": "", "source": NOT_FOUND}
    headline = str(event.get("headline") or "")
    sources = event.get("sources") or []
    publisher = (urlsplit(str(sources[0])).hostname or "") if sources else ""
    return {
        "amount": str(event.get("round_amount") or ""),
        "round": extract_funding_round(headline) or round_hint,
        "date": str(event.get("date") or ""),
        "source": f"funding event {event.get('event_id', '')} ({publisher or 'stored signal'})",
    }
