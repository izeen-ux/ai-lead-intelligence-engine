"""Phase 10: OpenAI adapter (Responses API) - request contract, strict structured output, typed
errors, bounded retry, budgets, secrecy, persistence. httpx.MockTransport only - never OpenAI."""

from __future__ import annotations

import json
import logging
import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.errors import (
    ProviderBudgetExceededError,
    ProviderCallBlockedError,
    ProviderError,
    ProviderIncompleteResponseError,
    ProviderMalformedResponseError,
    ProviderModelUnavailableError,
    ProviderNotConfiguredError,
    ProviderQuotaExhaustedError,
    ProviderRateLimitedError,
    ProviderRefusalError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.demo import CORE_DEMO_PAGES, build_demo_world
from app.domain.analysis.output import LeadAnalysisOutput
from app.domain.analysis.records import AnalysisStatus
from app.domain.enums import ProviderName
from app.main import create_app
from app.providers.ai_router.mock import mock_analysis
from app.providers.contracts import ChatMessage, ChatRequest, LlmReviewRequest
from app.providers.openai import OpenAIAdapter
from app.providers.openai.schema import strict_schema, text_format
from app.repositories.analysis import InMemoryAnalysisStore
from app.services.lead_analysis import LeadAnalysisService

KEY = "sk-test-openai-key-never-logged-0123456789"
MODEL = "gpt-6-luna"
REQ = ChatRequest(messages=[ChatMessage(role="user", content="Say hello")])


def completed(text: str = "OK", **over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "resp_1", "object": "response", "created_at": 1, "status": "completed",
        "model": f"{MODEL}-2026-08-01", "parallel_tool_calls": True, "tool_choice": "auto",
        "tools": [],
        "output": [{"type": "message", "id": "msg_1", "status": "completed", "role": "assistant",
                    "content": [{"type": "output_text", "text": text, "annotations": []}]}],
        "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                  "input_tokens_details": {"cached_tokens": 0},
                  "output_tokens_details": {"reasoning_tokens": 0}},
    }  # fmt: skip
    body.update(over)
    return body


def refused(text: str = "I can't help") -> dict[str, Any]:
    part = {"type": "refusal", "refusal": text}
    message = {"type": "message", "id": "m", "status": "completed", "role": "assistant"}
    return completed(output=[{**message, "content": [part]}])


def api_error(
    message: str = "nope", code: str | None = None, param: str | None = None
) -> dict[str, Any]:
    return {"error": {"message": message, "type": "invalid_request_error", "code": code,
                      "param": param}}  # fmt: skip


class Api:
    """Fake OpenAI: GET /models/<id> + scripted POST /responses (last step repeats)."""

    def __init__(self, *steps: Any, model_status: int = 200) -> None:
        self.steps = list(steps) or [(200, completed())]
        self.model_status = model_status
        self.requests: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if req.method == "GET" and "/models/" in req.url.path:
            if self.model_status != 200:
                missing = api_error("no such model", "model_not_found")
                return httpx.Response(self.model_status, json=missing)
            return httpx.Response(200, json={"id": MODEL, "object": "model", "created": 1,
                                             "owned_by": "openai"})  # fmt: skip
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, Exception):
            raise step
        status, body = step
        if isinstance(body, str):
            return httpx.Response(status, text=body, headers={"content-type": "text/html"})
        return httpx.Response(status, json=body)

    def posts(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.method == "POST"]

    def gets(self) -> int:
        return sum(1 for r in self.requests if r.method == "GET")


def adapter(make_settings, api: Api, **kw: Any) -> tuple[OpenAIAdapter, list[float]]:
    base: dict[str, Any] = {"provider_mode": "live", "allow_paid_provider_calls": True,
                            "openai_api_key": KEY, "openai_max_retries": 2}  # fmt: skip
    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)

    client = httpx.AsyncClient(transport=httpx.MockTransport(api))
    return OpenAIAdapter(make_settings(**(base | kw)), http_client=client, sleep=sleep), sleeps


# ================================================================== guards
async def test_missing_key_and_paid_guard_block_before_any_request(make_settings) -> None:
    api = Api()
    with pytest.raises(ProviderCallBlockedError):
        await adapter(make_settings, api, allow_paid_provider_calls=False)[0].complete(REQ)
    with pytest.raises(ProviderNotConfiguredError):
        await adapter(make_settings, api, openai_api_key=None)[0].complete(REQ)
    with pytest.raises(ProviderNotConfiguredError):
        await adapter(make_settings, api, openai_api_key="   ")[0].check_model()
    assert api.requests == []


def test_status_is_config_only(make_settings) -> None:
    api = Api()
    ok = adapter(make_settings, api)[0].status()
    assert (ok.name, ok.mode, ok.configured, ok.operational, ok.paid) == (
        ProviderName.OPENAI, "live", True, True, True,
    )  # fmt: skip
    assert ok.detail == f"model {MODEL}" and KEY not in ok.model_dump_json()
    missing = adapter(make_settings, api, openai_api_key=None)[0].status()
    assert (missing.configured, missing.operational) == (False, False)
    assert missing.detail == "OPENAI_API_KEY not set"
    unknown = adapter(make_settings, api, openai_model="brand-new-model")[0].status()
    assert unknown.operational is False and "price unknown" in (unknown.detail or "")
    assert api.requests == []


# ================================================================== model availability
async def test_model_is_validated_once_before_generation(make_settings) -> None:
    api = Api()
    a, _ = adapter(make_settings, api)
    await a.complete(REQ)
    await a.complete(REQ)
    assert api.gets() == 1 and len(api.posts()) == 2
    assert api.requests[0].url.path.endswith(f"/models/{MODEL}")


async def test_unavailable_model_stops_before_generation(make_settings) -> None:
    api = Api(model_status=404)
    with pytest.raises(ProviderModelUnavailableError):
        await adapter(make_settings, api)[0].complete(REQ)
    assert api.posts() == []


async def test_model_rejected_by_generation_endpoint(make_settings) -> None:
    api = Api((400, api_error("unsupported", param="model")))
    with pytest.raises(ProviderModelUnavailableError):
        await adapter(make_settings, api)[0].complete(REQ)
    assert len(api.posts()) == 1  # not retried


# ================================================================== happy path
async def test_responses_api_request_and_result(make_settings) -> None:
    api = Api()
    a, sleeps = adapter(make_settings, api)
    result = await a.complete(REQ)
    post = next(r for r in api.requests if r.method == "POST")
    assert post.url.path.endswith("/responses")
    assert post.headers["Authorization"] == f"Bearer {KEY}"
    body = api.posts()[0]
    assert body["model"] == MODEL and body["store"] is False
    assert body["input"] == [{"role": "user", "content": "Say hello"}]
    assert body["max_output_tokens"] == 2500
    assert "tools" not in body and "temperature" not in body and "text" not in body
    assert (result.provider, result.content, result.attempts) == (ProviderName.OPENAI, "OK", 1)
    assert result.model == f"{MODEL}-2026-08-01" and result.requested_model == MODEL
    assert (result.gateway, result.upstream_provider) == ("openai", "openai")
    u = result.usage
    assert (u.prompt_tokens, u.completion_tokens, u.total_tokens) == (100, 20, 120)
    assert result.cost_usd == Decimal("0.000020")  # 100 x $0.10/1M + 20 x $0.50/1M
    assert sleeps == []


async def test_reasoning_effort_is_sent_only_when_configured(make_settings) -> None:
    api = Api()
    await adapter(make_settings, api, openai_reasoning_effort="low")[0].complete(REQ)
    assert api.posts()[0]["reasoning"] == {"effort": "low"}


async def test_price_override_drives_cost(make_settings) -> None:
    a, _ = adapter(make_settings, Api(), openai_model="brand-new-model",
                   openai_price_input_per_1m_usd=Decimal(10),
                   openai_price_output_per_1m_usd=Decimal(100),
                   openai_max_estimated_cost_usd_per_run=Decimal(1))  # fmt: skip
    assert (await a.complete(REQ)).cost_usd == Decimal("0.003000")


# ================================================================== structured output
def _objects(node: Any) -> list[dict[str, Any]]:
    found = []
    if isinstance(node, dict):
        if "properties" in node:
            found.append(node)
        for value in node.values():
            found += _objects(value)
    elif isinstance(node, list):
        for item in node:
            found += _objects(item)
    return found


def test_strict_schema_from_the_domain_schema() -> None:
    domain = LeadAnalysisOutput.model_json_schema()
    schema = strict_schema(domain)
    objects = _objects(schema)
    assert len(objects) >= 5  # root + Fact, Inference, Hypothesis, Opportunity
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert obj["required"] == list(obj["properties"])
    text = json.dumps(schema)
    for key in ("minLength", "maxLength", "minItems", "maxItems", "minimum", "maximum",
                "title", "default"):  # fmt: skip
        assert f'"{key}":' not in text  # constraint KEYWORDS are gone, kept as description hints
    assert "max 400 characters" in text and "at least 1 item(s)" in text
    assert set(schema["properties"]) == set(domain["properties"])  # domain schema authoritative
    assert "minLength" in json.dumps(domain)  # the input was not mutated
    with pytest.raises(ValueError, match="object schema"):
        strict_schema({"type": "string"})


async def test_structured_output_request_format(make_settings) -> None:
    api = Api((200, completed('{"a": 1}')))
    req = REQ.model_copy(update={"response_schema": LeadAnalysisOutput.model_json_schema(),
                                 "response_schema_name": "lead_analysis"})  # fmt: skip
    await adapter(make_settings, api)[0].complete(req)
    fmt = api.posts()[0]["text"]["format"]
    assert (fmt["type"], fmt["name"], fmt["strict"]) == ("json_schema", "lead_analysis", True)
    assert fmt == text_format("lead_analysis", LeadAnalysisOutput.model_json_schema())["format"]


# ================================================================== unusable answers
async def test_refusal_is_typed_not_retried_and_reports_spend(make_settings) -> None:
    refusal = refused()
    api = Api((200, refusal))
    with pytest.raises(ProviderRefusalError) as exc:
        await adapter(make_settings, api)[0].complete(REQ)
    assert len(api.posts()) == 1
    assert exc.value.details["usage"]["total_tokens"] == 120
    assert exc.value.details["cost_usd"] == "0.000020"


async def test_incomplete_response(make_settings) -> None:
    api = Api((200, completed("partial {", status="incomplete",
                              incomplete_details={"reason": "max_output_tokens"})))  # fmt: skip
    with pytest.raises(ProviderIncompleteResponseError, match="max_output_tokens") as exc:
        await adapter(make_settings, api)[0].complete(REQ)
    assert exc.value.code == "provider_incomplete_response" and len(api.posts()) == 1


@pytest.mark.parametrize(
    "body",
    [completed(""), completed(output=[]), completed("x", status="failed"), "<html>oops</html>"],
)
async def test_malformed_or_empty_output(make_settings, body: Any) -> None:
    with pytest.raises(ProviderMalformedResponseError):
        await adapter(make_settings, Api((200, body)))[0].complete(REQ)


# ================================================================== errors + retry
@pytest.mark.parametrize("status", [401, 403])
async def test_auth_errors_are_not_retried(make_settings, status: int) -> None:
    api = Api((status, api_error("Incorrect API key provided: sk-...6789", "invalid_api_key")))
    a, sleeps = adapter(make_settings, api)
    with pytest.raises(ProviderNotConfiguredError) as exc:
        await a.complete(REQ)
    assert len(api.posts()) == 1 and sleeps == []
    assert KEY not in str(exc.value) and KEY not in json.dumps(exc.value.details)


@pytest.mark.parametrize(
    ("step", "error"),
    [
        ((429, api_error("slow down", "rate_limit_exceeded")), ProviderRateLimitedError),
        ((500, api_error("boom")), ProviderUnavailableError),
        ((503, api_error("overloaded")), ProviderUnavailableError),
        (httpx.ReadTimeout("slow"), ProviderTimeoutError),
        (httpx.ConnectError("down"), ProviderUnavailableError),
    ],
)
async def test_transient_errors_retry_with_bounded_backoff(make_settings, step, error) -> None:
    api = Api(step)
    a, sleeps = adapter(make_settings, api)
    with pytest.raises(error):
        await a.complete(REQ)
    assert len(api.posts()) == 3 and sleeps == [2.0, 4.0]  # 1 try + OPENAI_MAX_RETRIES=2


async def test_retry_then_success(make_settings) -> None:
    api = Api((429, api_error("slow", "rate_limit_exceeded")), (200, completed()))
    result = await adapter(make_settings, api)[0].complete(REQ)
    assert result.attempts == 2


async def test_quota_exhausted_is_not_retried(make_settings) -> None:
    api = Api((429, api_error("You exceeded your current quota", "insufficient_quota")))
    a, sleeps = adapter(make_settings, api)
    with pytest.raises(ProviderQuotaExhaustedError):
        await a.complete(REQ)
    assert len(api.posts()) == 1 and sleeps == []


async def test_other_4xx_is_a_hard_error(make_settings) -> None:
    api = Api((422, api_error("bad input")))
    with pytest.raises(ProviderError) as exc:
        await adapter(make_settings, api)[0].complete(REQ)
    assert exc.value.code == "provider_error" and len(api.posts()) == 1


# ================================================================== budgets
async def test_per_call_budget_blocks_before_any_request(make_settings) -> None:
    api = Api()
    a, _ = adapter(make_settings, api, openai_max_estimated_cost_usd_per_run=Decimal(0))
    with pytest.raises(ProviderBudgetExceededError) as exc:
        await a.complete(REQ)
    assert exc.value.code == "provider_budget_exceeded" and api.requests == []


async def test_unknown_price_fails_closed(make_settings) -> None:
    api = Api()
    with pytest.raises(ProviderNotConfiguredError, match="no price known"):
        await adapter(make_settings, api, openai_model="brand-new-model")[0].complete(REQ)
    assert api.requests == []


# ================================================================== secrecy
async def test_logs_are_useful_and_contain_no_secret_or_prompt(make_settings, caplog) -> None:
    caplog.set_level(logging.INFO)
    secret_prompt = ChatRequest(
        messages=[ChatMessage(role="user", content="CONFIDENTIAL-LEAD-PAYLOAD founder@x.example")],
        purpose="lead_analysis",
    )
    api = Api((500, api_error("boom")), (200, completed("ANSWER-TEXT")))
    await adapter(make_settings, api)[0].complete(secret_prompt)
    records = [r for r in caplog.records if r.getMessage() == "openai call"]
    assert [r.__dict__["status"] for r in records] == ["provider_unavailable", "ok"]
    last = records[-1].__dict__
    assert last["provider"] == "openai" and last["model"].startswith(MODEL)
    assert last["operation"] == "lead_analysis" and last["duration_ms"] >= 0
    assert last["usage_total"] == 120 and last["estimated_cost_usd"] == "0.000020"
    blob = caplog.text + json.dumps([{k: str(v) for k, v in r.__dict__.items()} for r in records])
    assert KEY not in blob and "CONFIDENTIAL-LEAD-PAYLOAD" not in blob and "ANSWER-TEXT" not in blob


def test_no_secret_in_readiness_or_dashboard(make_settings) -> None:
    settings = make_settings(provider_mode="live", openai_api_key=KEY)
    with TestClient(create_app(settings)) as client:
        ready = client.get("/health/ready")
        assert KEY not in ready.text
        providers = {p["name"]: p for p in ready.json()["providers"]}
        assert providers["openai"]["configured"] is True and "ai_router" not in providers
        assert KEY not in client.get("/health").text


async def test_review_is_one_structured_call_and_is_parsed(make_settings) -> None:
    """AI review of REVIEW cases (owner decision 2026-10-13)."""
    answer = {
        "decision": "qualified",
        "confidence": 0.91,
        "reasons": ["news and website both describe a sneaker brand"],
        "supporting_evidence_ids": ["evt_1"],
    }
    api = Api((200, completed(json.dumps(answer))))
    a, _ = adapter(make_settings, api)
    result = await a.review(
        LlmReviewRequest(
            subject_id=uuid.uuid4(),
            bundle={"company": "Zaydn", "domain": "zaydns.com", "events": [{"event_id": "evt_1"}]},
            allowed_evidence_ids=["evt_1"],
            qualified_allowed=True,
            task="attribution",
            company_profile={"website_title": "Zaydn - sneakers for Gen Z"},
            question_event_ids=["evt_1"],
        )
    )
    assert (result.decision.value, result.confidence) == ("qualified", 0.91)
    assert result.supporting_evidence_ids == ["evt_1"] and result.provider.value == "openai"


async def test_a_malformed_review_answer_is_a_provider_error(make_settings) -> None:
    a, _ = adapter(make_settings, Api((200, completed("not json"))))
    with pytest.raises(ProviderMalformedResponseError):
        await a.review(
            LlmReviewRequest(subject_id=uuid.uuid4(), bundle={}, qualified_allowed=False)
        )


# ================================================================== persistence through the service
def analysis_api(*, refuse_second: bool = False) -> Api:
    """Answers each analysis request with a grounded analysis built from the request itself."""
    api = Api()
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        api.requests.append(req)
        if req.method == "GET":
            return httpx.Response(200, json={"id": MODEL, "object": "model", "created": 1,
                                             "owned_by": "openai"})  # fmt: skip
        calls["n"] += 1
        if refuse_second and calls["n"] == 2:
            body = refused("no")
            return httpx.Response(200, json=body)
        user = json.loads(json.loads(req.content)["input"][-1]["content"])
        return httpx.Response(200, json=completed(json.dumps(mock_analysis(user))))

    api.__call__ = handler  # type: ignore[method-assign]
    api.handler = handler  # type: ignore[attr-defined]
    return api


async def test_provider_model_usage_and_cost_are_persisted(make_settings) -> None:
    settings = make_settings(provider_mode="live", allow_paid_provider_calls=True,
                             openai_api_key=KEY)  # fmt: skip
    world = await build_demo_world(make_settings(), dict(CORE_DEMO_PAGES))
    api = analysis_api(refuse_second=True)
    client = httpx.AsyncClient(transport=httpx.MockTransport(api.handler))  # type: ignore[attr-defined]
    provider = OpenAIAdapter(settings, http_client=client)
    store = InMemoryAnalysisStore()
    service = LeadAnalysisService(store, world.qualifications, world.enrichment, provider, settings)
    run, records = await service.run(world.company_ids)
    done = [r for r in records if r.status is AnalysisStatus.SUCCEEDED]
    failed = [r for r in records if r.status is AnalysisStatus.FAILED]
    assert len(done) == 1 and len(failed) == 1
    ok = done[0]
    assert (ok.provider, ok.gateway) == ("openai", "openai")
    assert ok.model == f"{MODEL}-2026-08-01" and ok.usage["total_tokens"] == 120
    assert ok.cost_usd == Decimal("0.000020") and ok.analysis is not None
    bad = failed[0]
    assert bad.error_code == "provider_refused" and bad.analysis is None
    assert bad.usage["total_tokens"] == 120 and bad.cost_usd == Decimal("0.000020")  # billed
    assert run.mode == "live" and run.cost_usd == Decimal("0.000040")
    assert run.total_tokens == 240
    sent = json.loads(next(r for r in api.requests if r.method == "POST").content)
    assert sent["text"]["format"]["name"] == "lead_analysis"  # service asks for strict output
    assert KEY not in json.dumps([r.model_dump(mode="json") for r in records])
