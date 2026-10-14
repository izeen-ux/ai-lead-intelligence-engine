"""Phase 7 lead analysis: catalog, evidence bundle, strict output + grounding, service gates,
budgets, failure handling, immutable history, redaction, router-only AI boundary. Hermetic."""

from __future__ import annotations

import ast
import dataclasses
import json
import uuid
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.core.errors import (
    ProviderModelUnavailableError,
    ProviderNotConfiguredError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.demo import CORE_DEMO_PAGES, DemoWorld, build_demo_world
from app.domain.analysis.bundle import AnalysisBundle, AnalysisInput, build_bundle
from app.domain.analysis.catalog import ServiceCatalog, default_catalog
from app.domain.analysis.config import ANALYSIS_POLICY_VERSION, PROMPT_VERSION, AnalysisConfig
from app.domain.analysis.output import (
    AnalysisOutputError,
    LeadAnalysisOutput,
    parse_output,
    validate_output,
)
from app.domain.analysis.prompt import build_messages
from app.domain.analysis.records import AnalysisStatus, LeadAnalysisRecord
from app.domain.enums import ContactStatus, RunStatus
from app.providers.ai_router import MockAiRouterProvider
from app.providers.ai_router.mock import _OFFER_BY_TYPE, mock_analysis
from app.providers.contracts import ChatRequest, ChatResult
from app.repositories.analysis import InMemoryAnalysisStore
from app.services.lead_analysis import LeadAnalysisService

ROOT = Path(__file__).resolve().parents[2]
KEY = "tair_secret_value_that_must_never_leak_42"
OFFER_IDS = {
    "ai_hiring_rekroots",
    "ai_audit_nbfc",
    "enterprise_ai_implementation",
    "prototype_30_day",
    "ai_presales_agents",
    "healthcare_ai_workflows",
    "partner_model_agencies",
    "ai_automation",
    "ai_audit_general",
}


# ================================================================== helpers
@pytest.fixture
async def demo(make_settings) -> tuple[Settings, DemoWorld]:
    settings = make_settings(ai_router_api_key=KEY)
    return settings, await build_demo_world(settings, dict(CORE_DEMO_PAGES))


def by_name(world: DemoWorld, name: str) -> uuid.UUID:
    return next(c for c in world.company_ids if world.enrichment.load_company(c).name == name)


def bundle_for(settings: Settings, world: DemoWorld, name: str, **kw: Any) -> AnalysisBundle:
    cid = by_name(world, name)
    decision = world.qualifications.latest_decision(cid)
    assert decision is not None
    contact = next(c for c in world.enrichment.contacts(cid) if c.status is ContactStatus.VERIFIED)
    ctx = world.enrichment.load_company(cid)
    assert ctx.domain is not None
    data = AnalysisInput(
        cid, ctx.name, ctx.domain, decision, contact,
        world.qualifications.load(cid).signals, list(ctx.pages),
    )  # fmt: skip
    return build_bundle(dataclasses.replace(data, **kw), AnalysisConfig.from_settings(settings))


def valid_answer(bundle: AnalysisBundle, catalog: ServiceCatalog | None = None) -> dict[str, Any]:
    user = json.loads(build_messages(bundle, catalog or default_catalog())[1]["content"])
    return mock_analysis(user)


def check(answer: dict[str, Any] | str, bundle: AnalysisBundle) -> LeadAnalysisOutput:
    out = parse_output(answer if isinstance(answer, str) else json.dumps(answer))
    validate_output(out, bundle, default_catalog())
    return out


class Scripted:
    """ReasoningProvider double: each call consumes one outcome (the last one repeats).
    None -> mock answer; str -> raw content; callable(user) -> dict; Exception -> raised."""

    def __init__(self, settings: Settings, *outcomes: Any, cost: Decimal | None = None) -> None:
        self.mock = MockAiRouterProvider(settings)
        self.outcomes = list(outcomes) or [None]
        self.cost = cost
        self.calls = 0

    def status(self) -> Any:
        return self.mock.status()

    async def complete(self, request: ChatRequest) -> ChatResult:
        self.calls += 1
        step = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(step, Exception):
            raise step
        result = await self.mock.complete(request)
        if isinstance(step, str):
            result = result.model_copy(update={"content": step})
        elif callable(step):
            user = json.loads(request.messages[-1].content)
            result = result.model_copy(update={"content": json.dumps(step(user))})
        return result.model_copy(update={"cost_usd": self.cost})


def service(settings: Settings, world: DemoWorld, provider: Any = None, **kw: Any):
    store = InMemoryAnalysisStore()
    svc = LeadAnalysisService(
        store, world.qualifications, world.enrichment,
        provider or Scripted(settings), settings, **kw,
    )  # fmt: skip
    return svc, store


def mutate(fn: Callable[[dict[str, Any]], None]) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def step(user: dict[str, Any]) -> dict[str, Any]:
        answer = mock_analysis(user)
        fn(answer)
        return answer

    return step


# ================================================================== catalog
def test_canonical_catalog_has_the_nine_offers() -> None:
    catalog = default_catalog()
    assert catalog.offer_ids == OFFER_IDS and len(catalog.offers) == 9
    assert catalog.version and catalog.catalog_id == "technotribes-services"
    prompt = json.dumps(catalog.for_prompt())
    assert all(o in prompt for o in OFFER_IDS)
    assert "$" not in prompt  # no prices in the catalog the model sees


def test_catalog_rejects_duplicate_offer_ids() -> None:
    data = default_catalog().model_dump()
    data["offers"].append(data["offers"][0])
    with pytest.raises(ValueError, match="duplicate"):
        ServiceCatalog.model_validate(data)


def test_mock_offer_mapping_only_uses_catalog_offers() -> None:
    assert {offer for _, offer, _ in _OFFER_BY_TYPE} <= OFFER_IDS


# ================================================================== bundle
async def test_bundle_is_compact_grounded_and_has_no_contact_details(demo) -> None:
    settings, world = demo
    bundle = bundle_for(settings, world, "Ecoride Fleet")
    p = bundle.payload
    assert set(p) == {"company", "qualification", "events", "pages", "contact"}
    assert p["qualification"]["decision"] == "qualified"
    assert set(p["contact"]) == {"id", "name", "title", "role"}
    assert "@" not in json.dumps(p)  # no email anywhere
    assert {p["qualification"]["id"], p["contact"]["id"]} <= bundle.allowed_ids
    assert all(e["id"].startswith("evt_") for e in p["events"])
    assert bundle.evidence_count == len(bundle.allowed_ids)
    again = bundle_for(settings, world, "Ecoride Fleet")
    assert again.allowed_ids == bundle.allowed_ids  # stable ids across builds


async def test_bundle_deduplicates_duplicate_coverage(demo) -> None:
    settings, world = demo
    cid = by_name(world, "Quikcart")
    signals = world.qualifications.load(cid).signals
    assert signals
    copies = [
        dataclasses.replace(
            s,
            signal_id=uuid.uuid4(),
            source_id=uuid.uuid4(),
            url=s.url.rstrip("/") + "/syndicated",
            platform="other-wire.example",
        )
        for s in signals
    ]
    base = bundle_for(settings, world, "Quikcart")
    doubled = bundle_for(settings, world, "Quikcart", signals=signals + copies)
    assert len(doubled.event_ids) == len(base.event_ids)  # same events, not twice as many
    assert doubled.duplicate_signals_merged >= len(copies)
    assert len(doubled.signal_ids) == 2 * len(base.signal_ids)  # provenance kept


async def test_bundle_page_ids_are_deterministic_without_page_uuid(demo) -> None:
    settings, world = demo
    cid = by_name(world, "Ecoride Fleet")
    pages = [dataclasses.replace(p, page_id=None) for p in world.enrichment.load_company(cid).pages]
    a = bundle_for(settings, world, "Ecoride Fleet", pages=pages)
    b = bundle_for(settings, world, "Ecoride Fleet", pages=pages)
    assert [x["id"] for x in a.payload["pages"]] == [x["id"] for x in b.payload["pages"]]


# ================================================================== strict output + grounding
async def test_valid_answer_passes_and_sections_stay_distinct(demo) -> None:
    settings, world = demo
    bundle = bundle_for(settings, world, "Ecoride Fleet")
    out = check(valid_answer(bundle), bundle)
    assert out.recommended_offer_id in OFFER_IDS
    assert out.cited_ids() <= bundle.allowed_ids
    assert out.observed_facts and out.pain_hypotheses and out.ai_opportunities
    assert all(0 <= h.confidence <= 1 for h in out.pain_hypotheses)


async def test_fenced_json_is_accepted(demo) -> None:
    settings, world = demo
    bundle = bundle_for(settings, world, "Ecoride Fleet")
    check("Here you go:\n```json\n" + json.dumps(valid_answer(bundle)) + "\n```", bundle)


@pytest.mark.parametrize("content", ["no json here", "{not: valid,}", "", "```json\n{\n```"])
def test_malformed_json_is_rejected(content) -> None:
    with pytest.raises(AnalysisOutputError) as exc:
        parse_output(content)
    assert exc.value.kind == "malformed_json"


@pytest.mark.parametrize(
    "change",
    [
        lambda a: a.pop("recommended_offer_id"),  # missing field
        lambda a: a.pop("observed_facts"),
        lambda a: a.update(observed_facts=[]),  # at least one fact
        lambda a: a.update(extra_field="x"),  # unknown field
        lambda a: a.update(confidence=1.5),
        lambda a: a.update(primary_trigger="vibes"),
        lambda a: a["pain_hypotheses"][0].pop("confidence"),
        lambda a: a["observed_facts"][0].update(evidence_ids=[]),  # facts must cite
    ],
)
async def test_schema_violations_are_rejected(demo, change) -> None:
    settings, world = demo
    bundle = bundle_for(settings, world, "Ecoride Fleet")
    answer = valid_answer(bundle)
    change(answer)
    with pytest.raises(AnalysisOutputError) as exc:
        check(answer, bundle)
    assert exc.value.kind == "schema_violation"


def _contact_only(a: dict[str, Any], b: AnalysisBundle) -> None:
    a["observed_facts"][0]["evidence_ids"] = [b.payload["contact"]["id"]]


def _hypothesis_as_fact(a: dict[str, Any], b: AnalysisBundle) -> None:
    a["pain_hypotheses"][0]["statement"] = a["observed_facts"][0]["statement"]


GROUNDING_CASES: list[tuple[Callable[[dict[str, Any], AnalysisBundle], None], str]] = [
    (lambda a, b: a["observed_facts"][0].update(evidence_ids=["evt_0"]), "unsupported_evidence"),
    (lambda a, b: a.update(supporting_evidence_ids=["https://x.y/z"]), "unsupported_evidence"),
    (lambda a, b: a.update(recommended_offer_id="blockchain_consulting"), "invalid_offer"),
    (lambda a, b: a["ai_opportunities"][0].update(offer_id="free_pilot"), "invalid_offer"),
    (_contact_only, "grounding_violation"),
    (_hypothesis_as_fact, "grounding_violation"),
    (
        lambda a, b: a.update(outreach_hook="Mail ceo@ecoride.example"),
        "grounding_violation",
    ),
    (
        lambda a, b: a.update(outreach_hook="Call +91 98765 43210 now"),
        "grounding_violation",
    ),
]


@pytest.mark.parametrize(("change", "kind"), GROUNDING_CASES)
async def test_grounding_rules(demo, change, kind) -> None:
    settings, world = demo
    bundle = bundle_for(settings, world, "Ecoride Fleet")
    answer = valid_answer(bundle)
    change(answer, bundle)
    with pytest.raises(AnalysisOutputError) as exc:
        check(answer, bundle)
    assert exc.value.kind == kind


async def test_prompt_carries_versions_catalog_and_contract(demo) -> None:
    settings, world = demo
    bundle = bundle_for(settings, world, "Ecoride Fleet")
    system, user = build_messages(bundle, default_catalog())
    assert system["role"] == "system" and user["role"] == "user"
    body = json.loads(user["content"])
    assert body["prompt_version"] == PROMPT_VERSION
    assert {o["offer_id"] for o in body["catalog"]["offers"]} == OFFER_IDS
    assert body["evidence"] == bundle.payload
    assert "recommended_offer_id" in json.dumps(body["output_contract"])


# ================================================================== service: gates
async def test_demo_run_analyses_only_eligible_leads(demo) -> None:
    settings, world = demo
    provider = Scripted(settings)
    svc, store = service(settings, world, provider)
    run, records = await svc.run(world.company_ids)
    status = {r.company_name: r.status for r in records}
    assert status == {
        "Quikcart": AnalysisStatus.SUCCEEDED,
        "Ecoride Fleet": AnalysisStatus.SUCCEEDED,
        "Drivnly": AnalysisStatus.BLOCKED,
    }
    blocked = next(r for r in records if r.company_name == "Drivnly")
    assert blocked.error_code == "enrichment_not_allowed" and blocked.analysis is None
    assert provider.calls == 2  # REVIEW lead never reaches the model
    assert (run.status, run.succeeded_count, run.blocked_count) == (RunStatus.SUCCEEDED, 2, 1)
    assert len(store.records) == 3 and store.runs[run.id].status is RunStatus.SUCCEEDED


async def test_no_verified_contact_blocks(demo, monkeypatch) -> None:
    settings, world = demo
    original = world.enrichment.contacts

    def unverified(cid: uuid.UUID) -> list[Any]:
        return [c.model_copy(update={"status": ContactStatus.UNVERIFIED}) for c in original(cid)]

    monkeypatch.setattr(world.enrichment, "contacts", unverified)
    provider = Scripted(settings)
    svc, _ = service(settings, world, provider)
    _, records = await svc.run([by_name(world, "Ecoride Fleet")])
    assert records[0].status is AnalysisStatus.BLOCKED
    assert records[0].error_code == "no_usable_contact"
    assert records[0].qualification_id is not None and provider.calls == 0
    # explicit opt-in to unverified contacts (config) lets it through
    loose = settings.model_copy(update={"analysis_require_verified_contact": False})
    svc2, _ = service(loose, world, Scripted(loose))
    _, records2 = await svc2.run([by_name(world, "Ecoride Fleet")])
    assert records2[0].status is AnalysisStatus.SUCCEEDED


async def test_contact_from_an_older_decision_is_not_usable(demo, monkeypatch) -> None:
    settings, world = demo
    original = world.enrichment.contacts

    def stale(cid: uuid.UUID) -> list[Any]:
        return [c.model_copy(update={"qualification_id": uuid.uuid4()}) for c in original(cid)]

    monkeypatch.setattr(world.enrichment, "contacts", stale)
    svc, _ = service(settings, world)
    _, records = await svc.run([by_name(world, "Quikcart")])
    assert records[0].error_code == "no_usable_contact"


async def test_eligible_ids_come_from_the_store_when_none_given(demo) -> None:
    settings, world = demo
    svc, store = service(settings, world)
    store.eligible = [by_name(world, "Quikcart")]
    _, records = await svc.run(None)
    assert [r.company_name for r in records] == ["Quikcart"]


# ================================================================== service: provenance
async def test_success_persists_usage_versions_and_provenance(demo) -> None:
    settings, world = demo
    svc, store = service(settings, world, Scripted(settings, cost=Decimal(0)))
    run, records = await svc.run([by_name(world, "Ecoride Fleet")])
    r = store.records[0]
    assert r.status is AnalysisStatus.SUCCEEDED
    assert (r.model, r.gateway, r.provider) == ("mock/lead-analyst-0", "mock", "mock")
    assert r.usage["total_tokens"] == r.usage["prompt_tokens"] + r.usage["completion_tokens"] > 0
    assert (r.policy_version, r.prompt_version) == (ANALYSIS_POLICY_VERSION, PROMPT_VERSION)
    assert r.catalog_version == default_catalog().version
    assert r.qualification_id and r.contact_id and r.evidence_signal_ids
    assert set(r.supporting_evidence_ids) <= set(json.dumps(r.evidence_bundle).split('"'))
    assert r.recommended_offer_id in OFFER_IDS and r.analysis is not None
    assert run.total_tokens == r.usage["total_tokens"] and run.cost_usd == Decimal(0)
    assert records[0] == r


async def test_persist_false_writes_nothing(demo) -> None:
    settings, world = demo
    svc, store = service(settings, world, persist=False)
    _, records = await svc.run(world.company_ids)
    assert records and store.records == [] and store.runs == {}


async def test_history_is_append_only(demo) -> None:
    settings, world = demo
    svc, store = service(settings, world)
    cid = by_name(world, "Quikcart")
    await svc.run([cid])
    await svc.run([cid])
    assert len(store.analyses(cid)) == 2  # re-analysis adds a new record, never overwrites
    with pytest.raises(ValueError, match="append-only"):
        store.insert_analysis(store.records[0])


def test_record_invariants() -> None:
    base: dict[str, Any] = {
        "run_id": uuid.uuid4(), "company_id": uuid.uuid4(), "company_name": "X",
        "catalog_version": "v", "policy_version": "p", "prompt_version": "q",
        "qualification_id": None, "contact_id": None,
    }  # fmt: skip
    with pytest.raises(ValueError, match="SUCCEEDED analysis needs"):
        LeadAnalysisRecord(status=AnalysisStatus.SUCCEEDED, **base)  # ungrounded success
    with pytest.raises(ValueError, match="error_code"):
        LeadAnalysisRecord(status=AnalysisStatus.FAILED, **base)  # failure without a code


# ================================================================== service: failures
@pytest.mark.parametrize(
    ("content", "kind"),
    [
        ("Sorry, I cannot help with that.", "malformed_json"),
        ('{"observed_facts": []}', "schema_violation"),
    ],
)
async def test_bad_model_output_is_stored_as_rejected(demo, content, kind) -> None:
    settings, world = demo
    svc, store = service(settings, world, Scripted(settings, content))
    run, _ = await svc.run([by_name(world, "Ecoride Fleet")])
    r = store.records[0]
    assert (r.status, r.error_code) == (AnalysisStatus.REJECTED_OUTPUT, kind)
    assert r.raw_excerpt == content[:500] and r.analysis is None
    assert r.usage["total_tokens"] > 0  # usage of a rejected call is still recorded
    assert run.rejected_count == 1


async def test_low_confidence_is_rejected(demo) -> None:
    settings, world = demo
    strict = settings.model_copy(update={"analysis_min_confidence": 0.9})  # mock answers 0.6
    svc, store = service(strict, world, Scripted(strict))
    await svc.run([by_name(world, "Ecoride Fleet")])
    assert (store.records[0].status, store.records[0].error_code) == (
        AnalysisStatus.REJECTED_OUTPUT,
        "low_confidence",
    )


async def test_invalid_offer_and_unsupported_ids_are_rejected(demo) -> None:
    settings, world = demo
    provider = Scripted(
        settings,
        mutate(lambda a: a.update(recommended_offer_id="web3_strategy")),
        mutate(lambda a: a.update(supporting_evidence_ids=["evt_ffffffffffff"])),
    )
    svc, store = service(settings, world, provider)
    await svc.run([by_name(world, "Ecoride Fleet"), by_name(world, "Quikcart")])
    assert [r.error_code for r in store.records] == ["invalid_offer", "unsupported_evidence"]


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ProviderTimeoutError("ai_router", "timed out"), "provider_timeout"),
        (ProviderRateLimitedError("ai_router", "429"), "provider_rate_limited"),
        (ProviderUnavailableError("ai_router", "502"), "provider_unavailable"),
        (ProviderModelUnavailableError("ai_router", "no model"), "provider_model_unavailable"),
    ],
)
async def test_provider_errors_become_failed_records(demo, error, code) -> None:
    settings, world = demo
    svc, store = service(settings, world, Scripted(settings, error, None))
    run, _ = await svc.run([by_name(world, "Ecoride Fleet"), by_name(world, "Quikcart")])
    assert [r.status for r in store.records] == [AnalysisStatus.FAILED, AnalysisStatus.SUCCEEDED]
    assert store.records[0].error_code == code
    assert (run.failed_count, run.succeeded_count) == (1, 1)


async def test_not_configured_stops_the_whole_run(demo) -> None:
    settings, world = demo
    provider = Scripted(settings, ProviderNotConfiguredError("ai_router", "401 Missing API key"))
    svc, store = service(settings, world, provider)
    with pytest.raises(ProviderNotConfiguredError):
        await svc.run(world.company_ids)
    assert provider.calls == 1  # did not keep burning through leads
    [run] = store.runs.values()
    assert run.status is RunStatus.FAILED and run.error_code == "provider_not_configured"


# ================================================================== service: budgets
@pytest.mark.parametrize(
    ("overrides", "cost", "prior", "expected", "reason"),
    [
        ({"analysis_max_per_run": 1}, None, Decimal(0), 1, "analysis limit"),
        ({"openai_max_tokens_per_run": 1000}, None, Decimal(0), 1, "token budget"),
        (
            {"openai_max_estimated_cost_usd_per_run": Decimal(0)},
            Decimal("0.01"),
            Decimal(0),
            1,
            "cost budget per run",
        ),
        (
            {"openai_max_estimated_cost_usd_per_month": Decimal(1)},
            None,
            Decimal(5),
            0,
            "monthly",
        ),
    ],
)
async def test_budgets_stop_before_the_next_call(
    demo, overrides, cost, prior, expected, reason
) -> None:
    settings, world = demo
    settings = settings.model_copy(update=overrides)
    provider = Scripted(settings, cost=cost)
    svc, store = service(settings, world, provider)
    store.prior_month_cost = prior
    ids = [by_name(world, "Ecoride Fleet"), by_name(world, "Quikcart")]
    run, records = await svc.run(ids)
    assert provider.calls == expected and len(records) == expected
    assert run.error_code == "budget_exhausted" and reason in (run.error_message or "")


async def test_blocked_leads_do_not_consume_the_analysis_budget(demo) -> None:
    settings, world = demo
    settings = settings.model_copy(update={"analysis_max_per_run": 1})
    svc, _ = service(settings, world)
    ids = [by_name(world, "Drivnly"), by_name(world, "Quikcart")]
    _, records = await svc.run(ids)
    assert [r.status for r in records] == [AnalysisStatus.BLOCKED, AnalysisStatus.SUCCEEDED]


# ================================================================== security / boundary
async def test_secret_never_reaches_records_or_logs(demo, caplog) -> None:
    settings, world = demo
    provider = Scripted(settings, ProviderUnavailableError("ai_router", "upstream 502"), None)
    svc, store = service(settings, world, provider)
    await svc.run(world.company_ids)
    dumped = json.dumps([r.model_dump(mode="json") for r in store.records])
    dumped += json.dumps([r.model_dump(mode="json") for r in store.runs.values()])
    assert KEY not in dumped and KEY not in caplog.text
    assert KEY not in json.dumps(AnalysisConfig.from_settings(settings).snapshot())
    assert KEY not in repr(settings)


VENDOR_MODULES = ("openai", "anthropic", "google.generativeai", "google.genai", "groq", "litellm")
VENDOR_HOSTS = ("api.openai.com", "api.anthropic.com", "openrouter.ai/api", "generativelanguage")


def test_no_direct_vendor_ai_path_in_app_code() -> None:
    offenders = []
    for path in (ROOT / "app").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            offenders += [f"{path.name}: import {n}" for n in names if n.startswith(VENDOR_MODULES)]
        offenders += [f"{path.name}: {h}" for h in VENDOR_HOSTS if h in source]
    # The official OpenAI and Anthropic SDKs are allowed in exactly one place each - their
    # adapters (app/providers/openai, app/providers/anthropic).
    allowed = {
        "adapter.py: import openai",
        "adapter.py: import anthropic",
        "__init__.py: import app.providers.anthropic.adapter",
        "registry.py: import app.providers.anthropic",
    }
    claude = (ROOT / "app" / "providers" / "anthropic" / "adapter.py").read_text(encoding="utf-8")
    assert "import anthropic" in claude
    in_adapter = (ROOT / "app" / "providers" / "openai" / "adapter.py").read_text(encoding="utf-8")
    assert "import openai" in in_adapter
    offenders = [o for o in offenders if o not in allowed]
    assert offenders == []
