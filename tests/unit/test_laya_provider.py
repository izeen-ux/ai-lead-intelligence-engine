"""Laya: typed question contract, live HTTP adapter (MockTransport - never the network), mock."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx2
import pytest
from pydantic import ValidationError

from app.core.errors import (
    ProviderError,
    ProviderMalformedResponseError,
    ProviderNotConfiguredError,
    ProviderRateLimitedError,
    ProviderRunFailedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.domain.enums import LayaDecisionKind, QualificationDimension
from app.domain.qualification.bundle import LAYA_QUESTIONS, LayaVerdict, interpret
from app.domain.qualification.icp import IcpConfig
from app.providers.contracts import LayaQuestion, QualificationRequest, QualificationResult
from app.providers.laya import LayaAdapter, MockLayaProvider

ENDPOINT = "http://127.0.0.1:8765"


def request(state: dict[str, Any] | None = None) -> QualificationRequest:
    return QualificationRequest(
        subject_id=uuid.uuid4(), state=state or {"events": []}, questions=list(LAYA_QUESTIONS)
    )


def answers(**over: Any) -> dict[str, Any]:
    base = {
        "business_trigger": {"type": "choice", "choice": "funding", "answer_confidence": 0.82,
                             "probabilities": {"funding": 0.8, "none": 0.2}},
        "attribution": {"type": "noul", "noul": 0.91, "answer_confidence": 0.91},
        "company_fit": {"type": "noul", "noul": 0.88, "answer_confidence": 0.88},
        "evidence_quality": {"type": "score", "score": 1.7, "answer_confidence": 0.7},
    }  # fmt: skip
    base.update(over)
    return base


class Sidecar:
    """Records requests; replies like laya-serve 0.3.22."""

    def __init__(self, status: int = 200, body: Any = None, exc: Exception | None = None):
        self.status, self.exc = status, exc
        self.body = body if body is not None else {"model": "laya-rl-agent", "answers": answers()}
        self.requests: list[httpx2.Request] = []

    def __call__(self, req: httpx2.Request) -> httpx2.Response:
        self.requests.append(req)
        if self.exc is not None:
            raise self.exc
        if req.url.path == "/health":
            return httpx2.Response(200, json={"status": "ok", "loaded": ["typed-decisions"]})
        return httpx2.Response(self.status, json=self.body)


def adapter(make_settings, sidecar: Sidecar, **kw: Any) -> LayaAdapter:
    s = make_settings(provider_mode="live", laya_endpoint_url=ENDPOINT, **kw)
    return LayaAdapter(s, transport=httpx2.MockTransport(sidecar))


# ================================================================== question contract
def test_questions_serialize_to_upstream_shape() -> None:
    shaped = {q.dimension.value: q.to_laya() for q in LAYA_QUESTIONS}
    assert shaped["business_trigger"]["type"] == "choice"
    assert "none" in shaped["business_trigger"]["criteria"]
    assert shaped["attribution"]["type"] == "noul"
    assert set(shaped["attribution"]["criteria"]) == {"false", "true"}
    assert shaped["evidence_quality"]["type"] == "score"
    assert isinstance(shaped["evidence_quality"]["criteria"], list)


@pytest.mark.parametrize(
    ("kind", "criteria"),
    [
        (LayaDecisionKind.CHOICE, {"only": "one"}),
        (LayaDecisionKind.SCORE, ["single level"]),
        (LayaDecisionKind.PROBABILITY, {"yes": "a", "no": "b"}),
    ],
)
def test_malformed_questions_rejected(kind, criteria) -> None:
    with pytest.raises(ValidationError):
        LayaQuestion(
            dimension=QualificationDimension.COMPANY_FIT,
            kind=kind,
            instructions="x",
            criteria=criteria,
        )


def test_one_question_per_dimension() -> None:
    q = LAYA_QUESTIONS[1]
    with pytest.raises(ValidationError, match="one question per dimension"):
        QualificationRequest(subject_id=uuid.uuid4(), state={}, questions=[q, q])


# ================================================================== live adapter contract
async def test_live_request_shape_and_parsed_answers(make_settings) -> None:
    sidecar = Sidecar()
    result = await adapter(make_settings, sidecar).evaluate(request({"company": "Acme"}))
    [req] = sidecar.requests
    assert (req.method, req.url.path) == ("POST", "/v1/systemone")
    body = json.loads(req.content)
    assert body["model"] == "typed-decisions"
    assert body["state"] == {"company": "Acme"}
    assert set(body["questions"]) == {q.dimension.value for q in LAYA_QUESTIONS}
    assert "Authorization" not in req.headers
    assert result.model_version == "laya:laya-rl-agent"
    fit = result.output(QualificationDimension.COMPANY_FIT)
    assert fit is not None and fit.value == 0.88 and fit.confidence == 0.88
    trig = result.output(QualificationDimension.BUSINESS_TRIGGER)
    assert trig is not None and trig.value == "funding" and trig.probabilities["none"] == 0.2
    assert result.latency_ms is not None


async def test_bearer_token_sent_when_configured(make_settings) -> None:
    sidecar = Sidecar()
    await adapter(make_settings, sidecar, laya_api_key="local-sidecar-key").evaluate(request())
    assert sidecar.requests[0].headers["Authorization"] == "Bearer local-sidecar-key"


async def test_health(make_settings) -> None:
    sidecar = Sidecar()
    health = await adapter(make_settings, sidecar).health()
    assert health["status"] == "ok" and sidecar.requests[0].method == "GET"


@pytest.mark.parametrize(
    "bad",
    [
        {"business_trigger": {"choice": "not-an-option"}},
        {"attribution": {"noul": 1.4}},
        {"attribution": {"noul": True}},
        {"evidence_quality": {"score": 7}},
        {"company_fit": "0.9"},
    ],
)
async def test_invalid_answers_are_malformed(make_settings, bad) -> None:
    sidecar = Sidecar(body={"answers": answers(**bad)})
    with pytest.raises(ProviderMalformedResponseError):
        await adapter(make_settings, sidecar).evaluate(request())


@pytest.mark.parametrize("body", [{"model": "x"}, ["not", "an", "object"]])
async def test_missing_answers_are_malformed(make_settings, body) -> None:
    with pytest.raises(ProviderMalformedResponseError):
        await adapter(make_settings, Sidecar(body=body)).evaluate(request())


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, ProviderNotConfiguredError),
        (422, ProviderError),
        (400, ProviderError),
        (503, ProviderRateLimitedError),
        (500, ProviderRunFailedError),
    ],
)
async def test_http_errors_are_typed(make_settings, status, error) -> None:
    sidecar = Sidecar(status=status, body={"detail": "question 'x': bad"})
    with pytest.raises(error) as exc:
        await adapter(make_settings, sidecar).evaluate(request())
    assert exc.value.details["provider"] == "laya"


async def test_timeout_is_typed(make_settings) -> None:
    sidecar = Sidecar(exc=httpx2.ReadTimeout("slow"))
    with pytest.raises(ProviderTimeoutError, match="did not answer"):
        await adapter(make_settings, sidecar, laya_timeout_seconds=0.5).evaluate(request())


async def test_unreachable_sidecar_is_typed(make_settings) -> None:
    sidecar = Sidecar(exc=httpx2.ConnectError("refused"))
    with pytest.raises(ProviderUnavailableError, match="laya-serve running"):
        await adapter(make_settings, sidecar).evaluate(request())


async def test_non_json_response_is_malformed(make_settings) -> None:
    def handler(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text="<html>")

    s = make_settings(provider_mode="live", laya_endpoint_url=ENDPOINT)
    with pytest.raises(ProviderMalformedResponseError):
        await LayaAdapter(s, transport=httpx2.MockTransport(handler)).evaluate(request())


@pytest.mark.parametrize(
    "url", [None, "https://laya.example.com", "http://10.0.0.5:8000", "http://0.0.0.0:8000"]
)
async def test_endpoint_must_be_configured_and_loopback(make_settings, url) -> None:
    sidecar = Sidecar()
    s = make_settings(provider_mode="live", laya_endpoint_url=url)
    laya = LayaAdapter(s, transport=httpx2.MockTransport(sidecar))
    assert laya.status().operational is False
    with pytest.raises(ProviderNotConfiguredError):
        await laya.evaluate(request())
    assert sidecar.requests == []  # nothing left the process


def test_status_when_configured(make_settings) -> None:
    status = adapter(make_settings, Sidecar()).status()
    assert (status.configured, status.operational, status.paid) == (True, True, False)


# ================================================================== interpretation
def result_with(**over: Any) -> QualificationResult:
    from app.providers.laya.adapter import parse_answer

    raw = answers(**over)
    return QualificationResult(
        provider="laya",
        outputs=[parse_answer(q, raw[q.dimension.value]) for q in LAYA_QUESTIONS],
    )


@pytest.mark.parametrize(
    ("over", "verdict"),
    [
        ({}, LayaVerdict.STRONG_POSITIVE),
        ({"company_fit": {"noul": 0.2, "answer_confidence": 0.8}}, LayaVerdict.NEGATIVE),
        ({"attribution": {"noul": 0.1, "answer_confidence": 0.9}}, LayaVerdict.NEGATIVE),
        ({"business_trigger": {"choice": "none", "answer_confidence": 0.9}}, LayaVerdict.NEGATIVE),
        ({"business_trigger": {"choice": "none", "answer_confidence": 0.4}}, LayaVerdict.AMBIGUOUS),
        ({"company_fit": {"noul": 0.6, "answer_confidence": 0.6}}, LayaVerdict.AMBIGUOUS),
        ({"evidence_quality": {"score": 0.5, "answer_confidence": 0.6}}, LayaVerdict.AMBIGUOUS),
    ],
)
def test_interpretation_thresholds(over, verdict) -> None:
    assessment = interpret(result_with(**over), IcpConfig())
    assert assessment.verdict is verdict
    assert 0 <= assessment.confidence <= 1


def test_incomplete_answers_are_ambiguous() -> None:
    partial = QualificationResult(provider="laya", outputs=[])
    assert interpret(partial, IcpConfig()).verdict is LayaVerdict.AMBIGUOUS


# ================================================================== mock
async def test_mock_is_deterministic_and_bounded() -> None:
    state = {
        "events": [
            {"types": ["funding"], "attributed": True},
            {"types": ["expansion"], "attributed": True},
        ]
    }
    mock = MockLayaProvider()
    first = await mock.evaluate(request(state))
    second = await mock.evaluate(request(state))
    assert first == second
    assert first.model_version == "mock-laya-0"
    assert interpret(first, IcpConfig()).verdict is LayaVerdict.STRONG_POSITIVE
    empty = await mock.evaluate(request({"events": []}))
    assert interpret(empty, IcpConfig()).verdict is LayaVerdict.NEGATIVE
    assert mock.calls == ["evaluate"] * 3
