"""Live Laya adapter (Phase 5) - HTTP client for a self-hosted ``laya-serve`` sidecar.

Upstream verified 2026-09-30: PyPI ``laya`` 0.3.22 (Convai Innovations, Apache-2.0), installed
as an isolated uv tool (torch/transformers never enter this app's environment, ADR-040). The
sidecar is bound to 127.0.0.1 and loads the ``typed-decisions`` checkpoint on CPU. Contract read
from ``laya/serve.py`` + ``laya/agent.py`` of that release:

  GET  /health -> {"status": "ok", "loaded": [...], "device": "cpu", ...}
  POST /v1/systemone  (Authorization: Bearer <LAYA_API_KEY> only if the sidecar sets one)
       {"state": {...}, "model": "typed-decisions",
        "questions": {key: {"type": "choice"|"score"|"noul", "instructions": str,
                            "criteria": {label: desc} | [levels] | {"false": .., "true": ..}}}}
    -> {"model": str, "answers": {key: {"type", "choice"|"score"|"noul", "probabilities"?,
        "confidence", "answer_confidence", ...}}, "usage": {...}, "routing": {...}}
       400/422 = bad request (message names the question), 401 = auth, 503 = busy,
       500 = "inference failed".

Laya is free (local compute) and never calls external services; the adapter refuses any
non-loopback endpoint so evidence bundles cannot leave the machine by misconfiguration.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlsplit

import httpx2

from app.core.config import Settings
from app.core.errors import (
    ProviderError,
    ProviderMalformedResponseError,
    ProviderNotConfiguredError,
    ProviderRateLimitedError,
    ProviderRunFailedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.domain.enums import LayaDecisionKind, ProviderName
from app.domain.models import LayaOutput
from app.providers.base import LiveAdapterBase
from app.providers.contracts import (
    LayaQuestion,
    ProviderStatus,
    QualificationRequest,
    QualificationResult,
)

_VALUE_KEY = {
    LayaDecisionKind.CHOICE: "choice",
    LayaDecisionKind.SCORE: "score",
    LayaDecisionKind.PROBABILITY: "noul",
}


def _bounded(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return min(max(float(value), 0.0), 1.0)


def parse_answer(question: LayaQuestion, raw: Any) -> LayaOutput:
    """Validate one upstream answer against the question it answers."""
    if not isinstance(raw, dict):
        raise ValueError("answer is not an object")
    raw_value = raw.get(_VALUE_KEY[question.kind])
    value: str | float
    if question.kind is LayaDecisionKind.CHOICE:
        if not isinstance(raw_value, str) or raw_value not in question.criteria:
            raise ValueError(f"choice {raw_value!r} is not one of the offered labels")
        value = raw_value
    else:
        if isinstance(raw_value, bool) or not isinstance(raw_value, int | float):
            raise ValueError(f"{question.kind.value} {raw_value!r} is not a number")
        value = float(raw_value)
        top = len(question.criteria) - 1 if question.kind is LayaDecisionKind.SCORE else 1
        if not 0 <= value <= top:
            raise ValueError(f"{question.kind.value} {raw_value!r} outside 0..{top}")
    probs = raw.get("probabilities") or {}
    return LayaOutput(
        dimension=question.dimension,
        kind=question.kind,
        value=value,
        confidence=_bounded(raw.get("answer_confidence", raw.get("confidence"))),
        probabilities={str(k): p for k, v in probs.items() if (p := _bounded(v)) is not None}
        if isinstance(probs, dict)
        else {},
    )


class LayaAdapter(LiveAdapterBase):
    name = ProviderName.LAYA
    implemented_in_phase = 5
    operational = True

    def __init__(
        self, settings: Settings, *, transport: httpx2.AsyncBaseTransport | None = None
    ) -> None:
        super().__init__(settings)
        self._transport = transport

    # ------------------------------------------------------------------ configuration
    def _endpoint_problem(self) -> str | None:
        url = self._settings.laya_endpoint_url
        if not url:
            return "LAYA_ENDPOINT_URL not set (self-hosted laya-serve sidecar)"
        host = (urlsplit(url).hostname or "").lower()
        if not self._settings.is_private_service_host(host):
            return (
                "LAYA_ENDPOINT_URL must be a loopback address or a host listed in "
                f"INTERNAL_SERVICE_HOSTS (got host {host!r})"
            )
        return None

    def status(self) -> ProviderStatus:
        base = super().status()
        problem = self._endpoint_problem()
        return base.model_copy(
            update={
                "configured": problem is None,
                "operational": problem is None,
                "detail": problem,
            }
        )

    def _ensure_callable(self, operation: str) -> None:
        super()._ensure_callable(operation)
        problem = self._endpoint_problem()
        if problem is not None:
            raise ProviderNotConfiguredError(
                self.name.value, f"{operation}: {problem}", details={"operation": operation}
            )

    def _client(self) -> httpx2.AsyncClient:
        headers = {}
        key = self._settings.laya_api_key
        if key is not None and key.get_secret_value():
            headers["Authorization"] = f"Bearer {key.get_secret_value()}"
        return httpx2.AsyncClient(
            base_url=str(self._settings.laya_endpoint_url).rstrip("/"),
            headers=headers,
            timeout=self._settings.laya_timeout_seconds,
            transport=self._transport,
        )

    # ------------------------------------------------------------------ calls
    async def health(self) -> dict[str, Any]:
        """GET /health of the sidecar (no inference)."""
        self._ensure_callable("health")
        async with self._client() as client:
            resp = await self._send(client, "GET", "/health", None)
        body: dict[str, Any] = self._json(resp)
        return body

    async def evaluate(self, request: QualificationRequest) -> QualificationResult:
        self._ensure_callable("evaluate")
        payload = {
            "state": request.state,
            "model": self._settings.laya_checkpoint,
            "questions": {q.dimension.value: q.to_laya() for q in request.questions},
        }
        started = time.perf_counter()
        async with self._client() as client:
            resp = await self._send(client, "POST", "/v1/systemone", payload)
        latency = int((time.perf_counter() - started) * 1000)
        body = self._json(resp)
        answers = body.get("answers")
        if not isinstance(answers, dict):
            raise ProviderMalformedResponseError(self.name.value, "response has no 'answers'")
        outputs = []
        for q in request.questions:
            try:
                outputs.append(parse_answer(q, answers.get(q.dimension.value)))
            except ValueError as exc:
                raise ProviderMalformedResponseError(
                    self.name.value,
                    f"invalid answer for {q.dimension.value}: {exc}",
                    details={"dimension": q.dimension.value},
                ) from None
        model = body.get("model")
        return QualificationResult(
            provider=self.name,
            outputs=outputs,
            model_version=f"laya:{model}" if isinstance(model, str) and model else "laya",
            latency_ms=latency,
        )

    # ------------------------------------------------------------------ helpers
    async def _send(
        self, client: httpx2.AsyncClient, method: str, path: str, payload: dict[str, Any] | None
    ) -> httpx2.Response:
        try:
            resp = await client.request(method, path, json=payload)
        except httpx2.TimeoutException:
            raise ProviderTimeoutError(
                self.name.value,
                f"Laya sidecar did not answer within {self._settings.laya_timeout_seconds}s",
                details={"path": path},
            ) from None
        except httpx2.TransportError as exc:
            raise ProviderUnavailableError(
                self.name.value,
                f"Laya sidecar unreachable ({type(exc).__name__}); is laya-serve running?",
                details={"path": path},
            ) from None
        if resp.status_code == 401:
            raise ProviderNotConfiguredError(
                self.name.value, "Laya sidecar rejected the bearer token (LAYA_API_KEY)"
            )
        if resp.status_code == 503:
            raise ProviderRateLimitedError(self.name.value, "Laya sidecar busy (503)")
        if resp.status_code in (400, 422):
            raise ProviderError(
                self.name.value,
                f"Laya rejected the request ({resp.status_code}): {self._detail(resp)}",
                details={"http_status": resp.status_code},
            )
        if resp.status_code >= 400:
            raise ProviderRunFailedError(
                self.name.value,
                f"Laya sidecar error {resp.status_code}: {self._detail(resp)}",
                details={"http_status": resp.status_code},
            )
        return resp

    def _json(self, resp: httpx2.Response) -> dict[str, Any]:
        try:
            body = resp.json()
        except ValueError:
            raise ProviderMalformedResponseError(self.name.value, "response is not JSON") from None
        if not isinstance(body, dict):
            raise ProviderMalformedResponseError(self.name.value, "response is not an object")
        return body

    @staticmethod
    def _detail(resp: httpx2.Response) -> str:
        try:
            detail = resp.json().get("detail", "")
        except (ValueError, AttributeError):
            detail = ""
        return str(detail)[:300]
