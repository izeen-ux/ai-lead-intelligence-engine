"""Phase 8: Google Sheets writer HTTP contract (httpx2.MockTransport only - never Google) and
service-account handling without exposing credentials."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx2
import pytest

from app.core.errors import (
    ProviderError,
    ProviderMalformedResponseError,
    ProviderNotConfiguredError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.providers.google_sheets import (
    GoogleSheetsWriter,
    InMemorySheetsWriter,
    ServiceAccountTokenProvider,
    StaticTokenProvider,
    service_account_email,
)
from app.providers.google_sheets.client import a1, column_letter

TOKEN = "ya29.test-access-token-never-logged"
SID = "1SpreadsheetIdForTestsOnly_abcdef"


class Google:
    def __init__(self, *steps: Any) -> None:
        self.steps = list(steps) or [(200, {})]
        self.requests: list[httpx2.Request] = []

    def __call__(self, req: httpx2.Request) -> httpx2.Response:
        self.requests.append(req)
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, Exception):
            raise step
        status, body = step
        if isinstance(body, str):
            return httpx2.Response(status, text=body)
        return httpx2.Response(status, json=body)

    def body(self, i: int = -1) -> dict[str, Any]:
        return dict(json.loads(self.requests[i].content))


def writer(google: Google, retries: int = 2) -> tuple[GoogleSheetsWriter, list[float]]:
    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)

    w = GoogleSheetsWriter(
        SID, StaticTokenProvider(TOKEN, identity="bot@proj.iam.gserviceaccount.com"),
        max_retries=retries, transport=httpx2.MockTransport(google), sleep=sleep,
    )  # fmt: skip
    return w, sleeps


META = {
    "properties": {"title": "Lead Output"},
    "sheets": [{"properties": {"title": "Leads"}}, {"properties": {"title": "Phase4_Test"}}],
}


# ================================================================== contract
async def test_spreadsheet_identity_and_bearer() -> None:
    g = Google((200, META))
    w, _ = writer(g)
    info = await w.spreadsheet()
    assert (info.title, info.worksheets) == ("Lead Output", ["Leads", "Phase4_Test"])
    req = g.requests[0]
    assert req.method == "GET" and req.url.path == f"/v4/spreadsheets/{SID}"
    assert req.url.params["fields"] == "properties.title,sheets.properties.title"
    assert req.headers["Authorization"] == f"Bearer {TOKEN}"
    assert w.api_calls == 1 and w.identity == "bot@proj.iam.gserviceaccount.com"


async def test_add_worksheet_body() -> None:
    g = Google((200, {"replies": [{}]}))
    w, _ = writer(g)
    await w.add_worksheet("Review_Queue")
    assert g.requests[0].url.path.endswith(f"{SID}:batchUpdate")
    assert g.body() == {"requests": [{"addSheet": {"properties": {"title": "Review_Queue"}}}]}


async def test_read_values_range_and_shape() -> None:
    g = Google((200, {"range": "Leads!A1:Z", "values": [["lead_id", "x"], [], ["lead_1"]]}))
    w, _ = writer(g)
    values = await w.read_values("Lead's tab", 26)
    assert values == [["lead_id", "x"], [], ["lead_1"]]
    assert unquote(g.requests[0].url.raw_path.decode()).endswith("/values/'Lead''s tab'!A1:Z")
    empty = Google((200, {"range": "Leads!A1:Z"}))  # Google omits "values" for an empty tab
    assert await writer(empty)[0].read_values("Leads", 3) == []


async def test_write_rows_is_one_raw_batch() -> None:
    g = Google((200, {"totalUpdatedRows": 2}))
    w, _ = writer(g)
    await w.write_rows("Leads", [(1, ["lead_id"]), (5, ["=1+1", "x"])])
    assert g.requests[0].url.path.endswith(f"{SID}/values:batchUpdate")
    assert g.body() == {
        "valueInputOption": "RAW",  # formulas are stored as text, never evaluated
        "data": [
            {"range": "'Leads'!A1", "values": [["lead_id"]]},
            {"range": "'Leads'!A5", "values": [["=1+1", "x"]]},
        ],
    }
    await w.write_rows("Leads", [])  # nothing to write -> no request
    assert len(g.requests) == 1


async def test_partial_write_is_detected() -> None:
    w, _ = writer(Google((200, {"totalUpdatedRows": 1})))
    with pytest.raises(ProviderMalformedResponseError, match="different number"):
        await w.write_rows("Leads", [(2, ["a"]), (3, ["b"])])


def test_column_letters_and_a1() -> None:
    assert [column_letter(n) for n in (1, 26, 27, 52)] == ["A", "Z", "AA", "AZ"]
    assert a1("It's", "B2") == "'It''s'!B2"


# ================================================================== errors + retry
@pytest.mark.parametrize("status", [401, 403])
async def test_auth_errors_explain_sharing_and_never_leak_token(status: int) -> None:
    g = Google(
        (status, {"error": {"code": status, "message": "The caller does not have permission"}})
    )
    w, sleeps = writer(g)
    with pytest.raises(ProviderNotConfiguredError, match="service-account email") as exc:
        await w.spreadsheet()
    assert TOKEN not in str(exc.value) and TOKEN not in json.dumps(exc.value.details)
    assert len(g.requests) == 1 and sleeps == []  # not retried


async def test_missing_spreadsheet() -> None:
    w, _ = writer(Google((404, {"error": {"message": "Requested entity was not found."}})))
    with pytest.raises(ProviderNotConfiguredError, match="GOOGLE_SHEETS_SPREADSHEET_ID"):
        await w.spreadsheet()


async def test_bad_request_is_a_hard_failure() -> None:
    g = Google((400, {"error": {"message": "Unable to parse range"}}))
    w, sleeps = writer(g)
    with pytest.raises(ProviderError, match="Unable to parse range"):
        await w.read_values("Leads", 3)
    assert len(g.requests) == 1 and sleeps == []


@pytest.mark.parametrize(
    ("step", "error"),
    [
        ((429, {"error": {"message": "quota"}}), ProviderRateLimitedError),
        ((503, {"error": {"message": "backend"}}), ProviderUnavailableError),
        (httpx2.ReadTimeout("slow"), ProviderTimeoutError),
        (httpx2.ConnectError("down"), ProviderUnavailableError),
    ],
)
async def test_transient_errors_retry_with_bounded_backoff(step: Any, error: type) -> None:
    g = Google(step)
    w, sleeps = writer(g, retries=2)
    with pytest.raises(error):
        await w.spreadsheet()
    assert len(g.requests) == 3 and sleeps == [2.0, 4.0]


async def test_transient_then_success() -> None:
    g = Google((429, {}), (500, {}), (200, META))
    w, sleeps = writer(g, retries=2)
    assert (await w.spreadsheet()).title == "Lead Output"
    assert w.api_calls == 3 and sleeps == [2.0, 4.0]


@pytest.mark.parametrize("body", ["<html>", {"sheets": []}, {"properties": {}}])
async def test_malformed_metadata(body: Any) -> None:
    with pytest.raises(ProviderMalformedResponseError):
        await writer(Google((200, body)))[0].spreadsheet()


def test_blank_spreadsheet_id_is_refused() -> None:
    with pytest.raises(ProviderNotConfiguredError):
        GoogleSheetsWriter("  ", StaticTokenProvider(TOKEN))


# ================================================================== credentials
FAKE_KEY = "-----BEGIN PRIVATE KEY-----\nNOT-A-REAL-KEY-0123456789\n-----END PRIVATE KEY-----\n"


def key_file(tmp_path: Path, **extra: Any) -> Path:
    path = tmp_path / "sa.json"
    data = {
        "type": "service_account",
        "client_email": "bot@proj.iam.gserviceaccount.com",
        "private_key": FAKE_KEY,
        "private_key_id": "abc",
        "token_uri": "https://example.invalid/token",
    }
    path.write_text(json.dumps(data | extra), encoding="utf-8")
    return path


def test_service_account_email_reads_only_the_public_field(tmp_path: Path) -> None:
    assert service_account_email(key_file(tmp_path)) == "bot@proj.iam.gserviceaccount.com"
    assert service_account_email(tmp_path / "missing.json") is None
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert service_account_email(tmp_path / "bad.json") is None


async def test_missing_key_file_is_not_configured(tmp_path: Path) -> None:
    provider = ServiceAccountTokenProvider(tmp_path / "nope.json")
    with pytest.raises(ProviderNotConfiguredError, match="key file not found"):
        await provider.token()


async def test_unusable_key_never_echoes_key_material(tmp_path: Path) -> None:
    provider = ServiceAccountTokenProvider(key_file(tmp_path))  # fake key: google-auth fails
    with pytest.raises(ProviderNotConfiguredError) as exc:
        await provider.token()
    text = str(exc.value) + json.dumps(exc.value.details) + repr(exc.value.__cause__)
    assert "PRIVATE KEY" not in text and "NOT-A-REAL-KEY" not in text
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


# ================================================================== in-memory writer
async def test_in_memory_writer_behaves_like_sheets() -> None:
    w = InMemorySheetsWriter()
    await w.add_worksheet("T")
    await w.write_rows("T", [(1, ["h1", "h2"]), (3, ["k", ""])])
    assert await w.read_values("T", 2) == [["h1", "h2"], [], ["k"]]
    assert (await w.spreadsheet()).worksheets == ["T"] and w.keys("T") == ["k"]
    w.fail_next.append(ProviderUnavailableError("google_sheets", "boom"))
    with pytest.raises(ProviderUnavailableError):
        await w.read_values("T", 2)
