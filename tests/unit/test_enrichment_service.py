"""Phase 6 EnrichmentService: gate, waterfall, verification, cache, budgets, identity, history."""

from __future__ import annotations

import ast
import logging
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.errors import ProviderUnavailableError
from app.core.logging import configure_logging
from app.domain.enrichment.budget import BudgetLedger
from app.domain.enrichment.config import ENRICHMENT_POLICY_VERSION, EnrichmentConfig
from app.domain.enums import (
    AttemptStatus,
    ContactStatus,
    EmailVerificationStatus,
    ProviderName,
    ProviderOperation,
    RunStatus,
)
from app.domain.models import utcnow
from app.providers.contracts import EnrichmentOutcome, EnrichmentRequest
from app.providers.enrichment_kit.mock import MockEnrichmentKitProvider
from app.providers.registry import ProviderRegistry
from tests.enrichment_factories import LATER, company, team, world

ROOT = Path(__file__).resolve().parents[2]
CEO = team("Asha Rao - Founder & CEO")
S = ContactStatus


async def one(make_settings, people: str = CEO, **kw):
    w = await world(make_settings, company(people=people), **kw)
    run, contacts = await w.service().run()
    return w, run, contacts


# ================================================================== gate
@pytest.mark.parametrize("qualify", ["review", "unverified"])
async def test_gate_blocks_review_and_not_qualified_with_zero_provider_calls(
    make_settings, qualify
) -> None:
    w = await world(make_settings, company(people=CEO, qualify=qualify))
    run, [c] = await w.service().run(w.ids)
    assert c.status is S.BLOCKED
    assert c.qualification_id is None
    assert w.kit.calls == [] and w.hunter.calls == []
    assert w.estore.attempt_log == []
    assert run.blocked_count == 1


async def test_gate_blocks_never_qualified_and_policy_mismatch(make_settings) -> None:
    _, ctx = company(people=CEO)
    w = await world(make_settings)
    w.estore.add_company(ctx)
    _, [never] = await w.service().run([ctx.company_id])
    assert never.status is S.BLOCKED and "never been qualified" in never.reasons[0]

    w2 = await world(make_settings, company(people=CEO))
    w2.qstore.decisions[0] = w2.qstore.decisions[0].model_copy(update={"policy_version": "p5-old"})
    _, [old] = await w2.service().run(w2.ids)
    assert old.status is S.BLOCKED and "older policy" in old.reasons[0]
    assert w2.kit.calls == []


async def test_gate_blocks_when_icp_configuration_changed(make_settings) -> None:
    w = await world(make_settings, company(people=CEO))
    changed = make_settings(funding_cap_inr=Decimal(30_000_000))
    _, [c] = await w.service(settings=changed).run(w.ids)
    assert c.status is S.BLOCKED and "different ICP configuration" in c.reasons[0]
    assert w.kit.calls == []


# ================================================================== waterfall + verification
async def test_qualified_hunter_success_verified_with_provenance(make_settings) -> None:
    w, run, [c] = await one(make_settings)
    assert c.status is S.VERIFIED
    assert (c.full_name, c.title, c.role) == ("Asha Rao", "Founder & CEO", "founder")
    assert c.email == "asha.rao@acmerobotics.in"
    assert (c.provider, c.verification_provider) == (ProviderName.HUNTER, ProviderName.HUNTER)
    assert c.qualification_id == w.qstore.latest_decision(c.company_id).id
    assert c.provenance["email"]["provider"] == "hunter"
    assert c.provenance["identity"]["sources"] == ["https://acmerobotics.in/team"]
    assert c.provenance["title"] == "website"
    assert [r.providers for r in w.kit.requests] == [[ProviderName.HUNTER, ProviderName.APOLLO]]
    assert w.hunter.verified == ["asha.rao@acmerobotics.in"]
    ops = [(a.provider, a.operation, a.status) for a in w.estore.attempt_log]
    assert ops == [
        (ProviderName.HUNTER, ProviderOperation.EMAIL_FINDER, AttemptStatus.HIT),
        (ProviderName.HUNTER, ProviderOperation.EMAIL_VERIFIER, AttemptStatus.HIT),
    ]
    assert (run.hunter_credits, run.apollo_credits, run.sidecar_calls) == (2, 0, 2)
    assert run.estimated_cost_usd == Decimal("0.06")
    assert c.policy_version == ENRICHMENT_POLICY_VERSION


async def test_hunter_miss_falls_back_to_apollo(make_settings) -> None:
    _, run, [c] = await one(make_settings, team("Ravi Missra - CEO"))
    assert (c.status, c.provider) == (S.VERIFIED, ProviderName.APOLLO)
    assert c.linkedin_url == "https://www.linkedin.com/in/synthetic-fixture-ravi-missra"
    assert c.provenance["linkedin_url"] == "apollo"
    assert c.provenance["email"]["provider_says_verified"] is True  # kept, not trusted
    assert c.verification_provider is ProviderName.HUNTER  # Apollo's claim never counts
    assert (run.hunter_credits, run.apollo_credits) == (1, 1)


@pytest.mark.parametrize(
    ("person", "status", "verification"),
    [
        ("Asha Invalid - CEO", S.INVALID_EMAIL, EmailVerificationStatus.INVALID),
        ("Asha Acceptall - CEO", S.UNVERIFIED, EmailVerificationStatus.ACCEPT_ALL),
    ],
)
async def test_verification_failure_outcomes(make_settings, person, status, verification) -> None:
    _, _, [c] = await one(make_settings, team(person))
    assert (c.status, c.email_verification) == (status, verification)


@pytest.mark.parametrize(
    "person", ["Kiran Timeout - CEO", "Kiran Ratelimit - CEO", "Kiran Quota - CEO"]
)
async def test_typed_hunter_errors_are_recorded_and_apollo_fills_in(make_settings, person) -> None:
    w, _, [c] = await one(make_settings, team(person))
    assert (c.status, c.provider) == (S.VERIFIED, ProviderName.APOLLO)
    first = w.estore.attempt_log[0]
    assert (first.provider, first.status) == (ProviderName.HUNTER, AttemptStatus.ERROR)
    assert first.error_code in ("timeout", "rate_limited", "quota_exhausted")
    assert first.credits == 0


async def test_malformed_response_fails_the_contact_not_the_run(make_settings) -> None:
    w = await world(
        make_settings,
        company(people=team("Mala Malformed - CEO")),
        company("Beta Labs", people=CEO, domain="betalabs.in"),
    )
    run, contacts = await w.service().run(w.ids)
    assert [c.status for c in contacts] == [S.FAILED, S.VERIFIED]
    assert contacts[0].reasons[0] == "enrichment failed: provider_malformed_response"
    assert run.status is RunStatus.SUCCEEDED and run.failed_count == 1


async def test_sidecar_down_fails_the_run_fast(make_settings) -> None:
    class Down(MockEnrichmentKitProvider):
        async def enrich(self, request: EnrichmentRequest) -> EnrichmentOutcome:
            raise ProviderUnavailableError("enrichment_kit", "unreachable")

    w = await world(make_settings, company(people=CEO))
    w.kit = Down()
    with pytest.raises(ProviderUnavailableError):
        await w.service().run(w.ids)
    [run] = w.estore.runs.values()
    assert run.status is RunStatus.FAILED and run.error_code == "provider_unavailable"


@pytest.mark.parametrize(
    ("person", "reason"),
    [
        ("Pat Wrongperson - CEO", "person_mismatch"),
        ("Pat Mismatch - CEO", "company_domain_mismatch"),
    ],
)
async def test_person_and_domain_mismatch_are_rejected(make_settings, person, reason) -> None:
    """Hunter's early-exit result is rejected -> Apollo gets its turn (budgeted follow-up)."""
    w, run, [c] = await one(make_settings, team(person))
    assert f"rejected hunter: {reason}" in c.reasons
    assert (c.status, c.provider) == (S.VERIFIED, ProviderName.APOLLO)
    assert "someone.else" not in (c.email or "") and "otherco" not in (c.email or "")
    assert [r.providers for r in w.kit.requests] == [
        [ProviderName.HUNTER, ProviderName.APOLLO],
        [ProviderName.APOLLO],
    ]
    assert run.sidecar_calls == 3  # waterfall + Apollo follow-up + verification


async def test_email_domain_mismatch_is_rejected(make_settings) -> None:
    class OtherDomain(MockEnrichmentKitProvider):
        async def enrich(self, request: EnrichmentRequest) -> EnrichmentOutcome:
            out = await super().enrich(request)
            bad = out.attempts[0].model_copy(update={"email": "asha@gmail.com"})
            bad.fields = {**bad.fields, "company_domain": None}
            return EnrichmentOutcome(orchestrator=out.orchestrator, final=bad, attempts=[bad])

    w = await world(make_settings, company(people=CEO))
    w.kit = OtherDomain()
    _, [c] = await w.service().run(w.ids)
    assert (c.status, c.email) == (S.NOT_FOUND, None)
    assert "rejected hunter: email_domain_mismatch" in c.reasons


async def test_no_email_found(make_settings) -> None:
    w, _, [c] = await one(make_settings, team("Nina Nobody - CEO"))
    assert (c.status, c.email) == (S.NOT_FOUND, None)
    assert w.hunter.verified == []  # nothing to verify, no credit spent


async def test_confidence_thresholds(make_settings) -> None:
    _, _, [low] = await one(make_settings, team("Lee Lowconf - CEO"))
    assert low.status is S.NOT_FOUND
    assert "rejected hunter: below_min_confidence" in low.reasons
    _, _, [strict] = await one(make_settings, CEO, enrichment_accept_confidence=0.95)
    assert strict.status is S.UNVERIFIED  # verified VALID but below the accept threshold
    assert any("below accept threshold" in r for r in strict.reasons)


async def test_unverified_when_verification_errors(make_settings) -> None:
    class VTimeout(MockEnrichmentKitProvider):
        async def enrich(self, request: EnrichmentRequest) -> EnrichmentOutcome:
            out = await super().enrich(request)
            hit = out.attempts[0].model_copy(update={"email": "vtimeout.asha@acmerobotics.in"})
            return EnrichmentOutcome(orchestrator=out.orchestrator, final=hit, attempts=[hit])

    w = await world(make_settings, company(people=CEO))
    w.kit = VTimeout()
    _, [c] = await w.service().run(w.ids)
    assert (c.status, c.email_verification) == (S.UNVERIFIED, EmailVerificationStatus.UNKNOWN)
    assert "verification error: timeout" in c.reasons
    verify = w.estore.attempt_log[-1]
    assert (verify.status, verify.error_code, verify.credits) == (
        AttemptStatus.ERROR,
        "timeout",
        Decimal(0),
    )


async def test_fresh_hunter_finder_verification_skips_a_second_verify(make_settings) -> None:
    class Preverified(MockEnrichmentKitProvider):
        def __init__(self, date: str) -> None:
            super().__init__()
            self.date = date

        async def enrich(self, request: EnrichmentRequest) -> EnrichmentOutcome:
            out = await super().enrich(request)
            hit = out.attempts[0]
            hit.fields = {
                **hit.fields,
                "evidence": {"verification": {"status": "valid", "date": self.date}},
            }
            return out

    w = await world(make_settings, company(people=CEO))
    w.kit = Preverified((LATER - timedelta(days=3)).date().isoformat())
    _, [c] = await w.service().run(w.ids)
    assert c.status is S.VERIFIED and w.hunter.verified == []
    stale = await world(make_settings, company(people=CEO))
    stale.kit = Preverified((LATER - timedelta(days=90)).date().isoformat())
    _, [c2] = await stale.service().run(stale.ids)
    assert c2.status is S.VERIFIED and len(stale.hunter.verified) == 1


# ================================================================== cache + duplicates
async def test_cache_hit_avoids_all_calls_and_expiry_refreshes(make_settings) -> None:
    w = await world(make_settings, company(people=CEO))
    await w.service().run(w.ids)
    calls = (len(w.kit.requests), len(w.hunter.verified))
    run2, [cached] = await w.service(clock=lambda: LATER + timedelta(days=1)).run(w.ids)
    assert cached.from_cache and cached.status is S.VERIFIED
    assert (len(w.kit.requests), len(w.hunter.verified)) == calls
    assert (run2.hunter_credits, run2.cached_count) == (Decimal(0), 1)
    await w.service(clock=lambda: LATER + timedelta(days=31)).run(w.ids)  # cache TTL expired
    assert len(w.kit.requests) == calls[0] + 1


async def test_cached_result_with_expired_verification_is_reverified_only(make_settings) -> None:
    # the verifier stamps its result with the real clock: measure "later" from it too
    w = await world(
        make_settings,
        company(people=CEO),
        enrichment_verification_ttl_days=5,
        enrichment_cache_ttl_days=365,
    )
    await w.service().run(w.ids)
    _, [c] = await w.service(clock=lambda: utcnow() + timedelta(days=10)).run(w.ids)
    assert c.from_cache and c.status is S.VERIFIED
    assert len(w.kit.requests) == 1 and len(w.hunter.verified) == 2
    assert "verification expired" in c.reasons[0]


async def test_expired_verification_is_never_presented_as_fresh(make_settings) -> None:
    # the verifier stamps its result with the real clock: measure "later" from it too
    w = await world(
        make_settings,
        company(people=CEO),
        enrichment_verification_ttl_days=5,
        enrichment_cache_ttl_days=365,
    )
    await w.service().run(w.ids)
    no_budget = make_settings(
        enrichment_verification_ttl_days=5,
        enrichment_cache_ttl_days=365,
        enrichment_max_hunter_credits_per_run=0,
    )
    later = utcnow() + timedelta(days=10)
    _, [c] = await w.service(settings=no_budget, clock=lambda: later).run(w.ids)
    assert c.from_cache
    assert (c.status, c.email_verification) == (S.UNVERIFIED, EmailVerificationStatus.UNVERIFIED)
    assert any("verification skipped" in r for r in c.reasons)


async def test_duplicate_suppression_within_a_run(make_settings) -> None:
    w = await world(make_settings, company(people=CEO))
    _, contacts = await w.service().run([w.ids[0], w.ids[0]])
    assert len(contacts) == 1 and len(w.kit.requests) == 1


# ================================================================== budgets
async def test_budget_exhausted_stops_safely_before_any_call(make_settings) -> None:
    w = await world(
        make_settings, company(people=CEO),
        enrichment_max_hunter_credits_per_run=0, enrichment_max_apollo_credits_per_run=0,
    )  # fmt: skip
    run, contacts = await w.service().run(w.ids)
    assert contacts == [] and w.kit.requests == []
    assert run.error_code == "budget_exhausted"


async def test_hunter_budget_left_only_apollo_in_waterfall(make_settings) -> None:
    w = await world(make_settings, company(people=CEO), enrichment_max_hunter_credits_per_run=0)
    _, [c] = await w.service().run(w.ids)
    assert w.kit.requests[0].providers == [ProviderName.APOLLO]
    assert c.status is S.UNVERIFIED  # no Hunter credit left to verify
    assert any("verification skipped" in r for r in c.reasons)
    assert w.estore.attempt_log[-1].status is AttemptStatus.SKIPPED


async def test_monthly_budget_counts_prior_usage(make_settings) -> None:
    w = await world(make_settings, company(people=CEO), enrichment_max_hunter_credits_per_month=10,
                    enrichment_max_apollo_credits_per_month=0)  # fmt: skip
    w.estore.prior_month = {ProviderName.HUNTER: Decimal(10)}
    run, contacts = await w.service().run(w.ids)
    assert contacts == [] and run.error_code == "budget_exhausted"


def test_ledger_limits() -> None:
    cfg = EnrichmentConfig(max_attempts_per_lead=2, max_sidecar_calls_per_run=3,
                           max_cost_usd_per_run=Decimal("0.05"))  # fmt: skip
    ledger = BudgetLedger(cfg)
    assert ledger.allowed_waterfall("p") == [ProviderName.HUNTER, ProviderName.APOLLO]
    ledger.record(ProviderName.HUNTER, "p", Decimal(1), sidecar_call=True)
    assert ledger.blocked_reason(ProviderName.HUNTER, "q") == "run cost budget reached"
    ledger.record(None, "p", Decimal(0))
    assert ledger.blocked_reason(ProviderName.APOLLO, "p") == "per-lead attempt limit reached"
    ledger.sidecar_calls = 3
    assert ledger.blocked_reason(ProviderName.APOLLO, "z") == "sidecar call limit (run) reached"
    assert ledger.exhausted()
    assert ledger.snapshot()["run_credits"]["hunter"] == "1"


# ================================================================== identity outcomes
async def test_ambiguous_founder_is_recorded_and_never_called(make_settings) -> None:
    w, _, [c] = await one(make_settings, team("Asha Rao - CEO", "Asha Reddy - Co-Founder"))
    assert c.status is S.AMBIGUOUS and w.kit.requests == []
    assert c.qualification_id is not None


async def test_no_person_and_optional_hunter_people_search(make_settings) -> None:
    w, _, [c] = await one(make_settings, "We build robots.")
    assert c.status is S.NO_PERSON and w.kit.people_requests == []
    w2, run, [c2] = await one(make_settings, "We build robots.", enrichment_people_search="hunter")
    assert [r.domain for r in w2.kit.people_requests] == ["acmerobotics.in"]
    assert (c2.full_name, c2.status) == ("Asha Rao", S.VERIFIED)  # "Head of Sales" not eligible
    assert "hunter_domain_search:acmerobotics.in" in c2.provenance["identity"]["sources"]
    assert run.hunter_credits == 3  # domain search + finder + verify


async def test_multiple_founders_and_role_priority(make_settings) -> None:
    people = team("Meena Iyer - CTO", "Asha Rao - Founder & CEO", "Vik Nair - Head of Sales")
    _, _, contacts = await one(make_settings, people, enrichment_max_persons_per_company=2)
    assert [(c.full_name, c.role) for c in contacts] == [
        ("Asha Rao", "founder"),
        ("Meena Iyer", "cto"),
    ]
    assert any("Head of Sales" in r for r in contacts[0].reasons)


# ================================================================== history / safety
async def test_history_is_append_only_across_runs(make_settings) -> None:
    w = await world(make_settings, company(people=CEO))
    await w.service().run(w.ids)
    await w.service(clock=lambda: LATER + timedelta(days=1)).run(w.ids)
    assert len(w.estore.contact_log) == 2 and len(w.estore.runs) == 2
    with pytest.raises(ValueError, match="append-only"):
        w.estore.insert_contact(w.estore.contact_log[0])
    with pytest.raises(ValueError, match="append-only"):
        w.estore.insert_attempt(w.estore.attempt_log[0])


async def test_dry_run_persists_nothing(make_settings) -> None:
    w = await world(make_settings, company(people=CEO))
    _, [c] = await w.service(persist=False).run(w.ids)
    assert c.status is S.VERIFIED
    assert w.estore.contact_log == [] and w.estore.attempt_log == [] and w.estore.cache == {}


async def test_mock_mode_registry_refuses_paid_live_calls(make_settings) -> None:
    from app.core.errors import ProviderCallBlockedError

    live = ProviderRegistry(
        make_settings(
            provider_mode="live", hunter_api_key="k", enrichment_kit_base_url="http://127.0.0.1:1"
        )
    )
    with pytest.raises(ProviderCallBlockedError):
        await live.enrichment.enrich(
            EnrichmentRequest(
                lead_id=__import__("uuid").uuid4(),
                target="person",
                domain="a.in",
                person_name="A B",
            )
        )
    with pytest.raises(ProviderCallBlockedError):
        await live.email_verifier.verify_email("a@a.in")


def test_secrets_are_redacted_from_logs(make_settings, capsys) -> None:
    secret = "hunter-live-key-should-never-print"
    configure_logging(level="INFO", fmt="console", secret_values=[secret])
    logging.getLogger("app.test").warning("calling sidecar with key %s", secret)
    out = capsys.readouterr()
    assert secret not in out.out + out.err


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
    return found


def test_no_enrichment_path_without_the_qualification_gate() -> None:
    """Only the gated service (plus wiring: registry, CLI, smoke) may import enrichment vendors;
    the service imports the gate."""
    vendors = ("app.providers.enrichment_kit", "app.providers.hunter", "app.providers.apollo")
    allowed = {
        "app/providers/registry.py",
        "app/providers/hunter/adapter.py",
        "app/providers/apollo/adapter.py",
        "app/providers/enrichment_kit/adapter.py",
        "scripts/run_enrichment.py",
        "scripts/smoke_check.py",
        "app/demo.py",  # offline demo: mocks driven only through the gated service
        "app/services/health.py",
    }  # fmt: skip
    offenders = []
    for path in [*ROOT.glob("app/**/*.py"), *ROOT.glob("scripts/*.py")]:
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(vendors[0].replace(".", "/")) or rel in allowed:
            continue
        if rel.startswith(("app/providers/hunter/", "app/providers/apollo/")):
            continue
        if any(m.startswith(vendors) for m in _imports(path)):
            offenders.append(rel)
    assert offenders == []
    assert "app.services.enrichment_gate" in _imports(ROOT / "app/services/enrichment.py")
