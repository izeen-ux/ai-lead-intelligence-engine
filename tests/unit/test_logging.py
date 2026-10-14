from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from app.core.logging import REDACTED, configure_logging, request_id_var


@pytest.fixture(autouse=True)
def _restore_root_logger():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers, root.level = handlers, level


def _last_json_line(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    payload: dict[str, Any] = json.loads(lines[-1])
    return payload


def test_json_log_line_has_standard_fields_and_request_id(capsys) -> None:
    configure_logging(level="INFO", fmt="json")
    token = request_id_var.set("req-123")
    try:
        logging.getLogger("t").info("hello", extra={"lead_count": 3})
    finally:
        request_id_var.reset(token)
    payload = _last_json_line(capsys)
    assert payload["message"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["request_id"] == "req-123"
    assert payload["lead_count"] == 3


def test_secret_keys_and_known_secret_values_are_redacted(capsys) -> None:
    configure_logging(level="INFO", fmt="json", secret_values=["sk-live-abcdef123456"])
    logging.getLogger("t").info(
        "calling with sk-live-abcdef123456", extra={"api_key": "whatever", "note": "ok"}
    )
    out = _last_json_line(capsys)
    assert "sk-live-abcdef123456" not in json.dumps(out)
    assert out["api_key"] == REDACTED
    assert REDACTED in out["message"]
    assert out["note"] == "ok"


def test_console_format_and_level_filtering(capsys) -> None:
    configure_logging(level="WARNING", fmt="console")
    logging.getLogger("t").info("hidden")
    logging.getLogger("t").warning("shown", extra={"k": "v"})
    out = capsys.readouterr().out
    assert "hidden" not in out
    assert "shown" in out and "k=v" in out


def test_exceptions_are_serialized(capsys) -> None:
    configure_logging(fmt="json")
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("t").exception("failed")
    assert "ValueError: boom" in _last_json_line(capsys)["exception"]
