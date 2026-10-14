"""Final lead-output expansion: optional phone capability (default OFF, own budget), founder
LinkedIn provenance, and "never fabricate" behaviour for missing fields."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from app.domain.enrichment.budget import BudgetLedger
from app.domain.enrichment.config import EnrichmentConfig
from app.domain.enrichment.records import EnrichmentContactRecord
from app.domain.enums import ContactStatus, EnrichmentTarget, PhoneStatus, ProviderName
from app.providers.enrichment_kit.mapping import attempt_from_output, phone_of
from tests.enrichment_factories import company, team, world

CEO = team("Asha Rao - Founder & CEO")


def hunter_entry(**out: Any) -> dict[str, Any]:
    output: dict[str, Any] = {
        "email": "asha.rao@acmerobotics.in", "firstName": "Asha", "lastName": "Rao",
        "fullName": "Asha Rao", "title": "CEO", "companyDomain": "acmerobotics.in",
        "linkedinUrl": None, "confidence": 0.92, "evidence": {},
    }  # fmt: skip
    return {"provider": "hunter", "output": output | out, "latencyMs": 5, "called": True}


def attempt(entry: dict[str, Any], *, include_phone: bool) -> Any:
    return attempt_from_output(
        entry,
        lead_id=uuid.uuid4(),
        target=EnrichmentTarget.PERSON,
        orchestrator="enrichment_kit",
        include_phone=include_phone,
    )


# ------------------------------------------------------------------ phone: off by default
async def test_phone_is_not_requested_by_default(make_settings) -> None:
    w = await world(make_settings, company(people=CEO))
    _, [c] = await w.service().run()
    assert c.status is ContactStatus.VERIFIED
    assert (c.phone, c.phone_status) == (None, PhoneStatus.NOT_REQUESTED)
    assert [r.include_phone for r in w.kit.requests] == [False]
    assert c.provenance["phone"] == {"status": "not_requested", "provider": None}


def test_a_number_in_the_response_is_dropped_unless_requested() -> None:
    entry = hunter_entry(phone="+91 98765 43210")
    assert attempt(entry, include_phone=False).phone is None
    assert attempt(entry, include_phone=True).phone == "+91 98765 43210"


# ------------------------------------------------------------------ phone: enabled
async def test_enabled_phone_is_stored_with_status_and_provider(make_settings) -> None:
    w = await world(make_settings, company(people=CEO), enrichment_phone_enabled=True)
    run, [c] = await w.service().run()
    assert [r.include_phone for r in w.kit.requests] == [True]
    assert c.phone is not None and c.phone.startswith("+91 00000 ")  # the mock's synthetic range
    assert c.phone_status is PhoneStatus.FOUND
    assert c.provenance["phone"] == {"status": "found", "provider": "hunter"}
    assert run.estimated_cost_usd == w.settings.enrichment_hunter_credit_usd * 2  # no phone credit
    cached = next(iter(w.estore.cache.values()))
    assert (cached.phone, cached.phone_status) == (c.phone, PhoneStatus.FOUND)


async def test_phone_requested_but_provider_has_none_is_not_found(make_settings) -> None:
    # Hunter misses -> Apollo answers; Apollo's phone reveal is never requested.
    w = await world(
        make_settings, company(people=team("Ravi Missra - CEO")), enrichment_phone_enabled=True
    )
    _, [c] = await w.service().run()
    assert c.status is ContactStatus.VERIFIED and c.provider is ProviderName.APOLLO
    assert (c.phone, c.phone_status) == (None, PhoneStatus.NOT_FOUND)
    assert c.provenance["phone"] == {"status": "not_found", "provider": None}


async def test_phone_budget_is_separate_and_stops_requests(make_settings) -> None:
    w = await world(
        make_settings,
        company("Acme Robotics", people=CEO),
        company("Kheti Labs", people=team("Meera Shah - Founder"), domain="khetilabs.in"),
        enrichment_phone_enabled=True,
        enrichment_max_phone_lookups_per_run=1,
    )
    _, contacts = await w.service().run()
    assert [r.include_phone for r in w.kit.requests] == [True, False]
    assert [c.phone_status for c in contacts] == [PhoneStatus.FOUND, PhoneStatus.BUDGET_EXHAUSTED]
    assert contacts[1].phone is None and contacts[1].status is ContactStatus.VERIFIED


def test_ledger_reports_why_a_phone_is_not_requested() -> None:
    assert BudgetLedger(EnrichmentConfig()).phone_status() is PhoneStatus.NOT_REQUESTED
    on = BudgetLedger(EnrichmentConfig(phone_enabled=True, max_phone_lookups_per_run=0))
    assert on.phone_status() is PhoneStatus.BUDGET_EXHAUSTED
    assert BudgetLedger(EnrichmentConfig(phone_enabled=True)).phone_status() is None


@pytest.mark.parametrize(
    ("raw", "kept"),
    [
        ("+91 98765 43210", True),
        ("(080) 4123-4567", True),
        ("call us", False),
        ("12345", False),
        ("+91 98765 43210 ext 4 or mail hello@x.in", False),
        (None, False),
        (919876543210, False),
    ],
)
def test_only_plausible_numbers_are_kept(raw: Any, kept: bool) -> None:
    assert (phone_of(raw) is not None) is kept


def test_a_phone_cannot_be_recorded_without_the_found_status() -> None:
    base: dict[str, Any] = {
        "run_id": uuid.uuid4(), "company_id": uuid.uuid4(), "company_name": "Acme",
        "domain": "acme.in", "qualification_id": None, "status": ContactStatus.BLOCKED,
        "policy_version": "p",
    }  # fmt: skip
    with pytest.raises(ValidationError, match="phone"):
        EnrichmentContactRecord(**base, phone="+91 98765 43210")
    with pytest.raises(ValidationError, match="phone"):
        EnrichmentContactRecord(**base, phone_status=PhoneStatus.FOUND)


# ------------------------------------------------------------------ LinkedIn provenance
async def test_founder_linkedin_url_records_which_provider_returned_it(make_settings) -> None:
    w = await world(make_settings, company(people=CEO))
    _, [c] = await w.service().run()
    assert c.linkedin_url == "https://www.linkedin.com/in/synthetic-fixture-asha-rao"
    assert c.provenance["linkedin_url"] == "hunter"


def test_missing_linkedin_and_phone_stay_empty_never_invented() -> None:
    result = attempt(hunter_entry(linkedinUrl="  ", phone=None), include_phone=True)
    assert result.fields["linkedin_url"] is None and result.phone is None
    assert result.email == "asha.rao@acmerobotics.in"  # the rest of the result is unaffected
