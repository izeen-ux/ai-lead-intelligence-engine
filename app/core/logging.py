"""Structured logging on top of the standard library.

* ``json`` format: one JSON object per line (machine-parseable, default).
* ``console`` format: readable single line for local development.
* Every record carries the current ``request_id`` (if any).
* ``RedactionFilter`` masks secret-looking keys in ``extra`` fields and any known secret value
  that slips into a message.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterable
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

REDACTED = "***REDACTED***"

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
# Set by the pipeline orchestrator so every log line of a run carries its id and stage.
pipeline_run_var: ContextVar[str | None] = ContextVar("pipeline_run_id", default=None)
pipeline_stage_var: ContextVar[str | None] = ContextVar("pipeline_stage", default=None)

_SECRET_KEY_PATTERN = re.compile(
    r"(api[_-]?key|token|secret|password|passwd|authorization|credential|database_url|dsn)",
    re.IGNORECASE,
)
# Attributes every LogRecord has; anything else was passed through ``extra=``.
# ``color_message`` is uvicorn's ANSI-coloured duplicate of ``message``.
_STANDARD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()
    | {"message", "asctime", "color_message"}
)


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k, v in vars(record).items() if k not in _STANDARD_ATTRS}


class RedactionFilter(logging.Filter):
    def __init__(self, secret_values: Iterable[str] = ()) -> None:
        super().__init__()
        # Ignore very short values to avoid masking ordinary words.
        self._secret_values = tuple(v for v in secret_values if v and len(v) >= 6)

    def _scrub(self, text: str) -> str:
        for value in self._secret_values:
            text = text.replace(value, REDACTED)
        return text

    def _deep(self, value: Any) -> Any:
        """Scrub secret values inside nested extras (dicts / lists), and secret-named keys."""
        if isinstance(value, str):
            return self._scrub(value)
        if isinstance(value, dict):
            return {
                k: REDACTED if _SECRET_KEY_PATTERN.search(str(k)) else self._deep(v)
                for k, v in value.items()
            }
        if isinstance(value, list | tuple):
            return [self._deep(v) for v in value]
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in _extra_fields(record).items():
            if _SECRET_KEY_PATTERN.search(key):
                setattr(record, key, REDACTED)
            else:
                setattr(record, key, self._deep(value))
        if self._secret_values:
            record.msg = self._scrub(record.getMessage())
            record.args = None
            if record.exc_info:  # tracebacks can quote a secret (e.g. a URL with a key)
                record.exc_text = self._scrub(logging.Formatter().formatException(record.exc_info))
                record.exc_info = None
        return True


class RequestIdFilter(logging.Filter):
    """Adds the correlation context: request id, and pipeline run id / stage when inside a run."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        run_id, stage = pipeline_run_var.get(), pipeline_stage_var.get()
        if run_id is not None and not hasattr(record, "pipeline_run_id"):
            record.pipeline_run_id = run_id
        if stage is not None and not hasattr(record, "stage"):
            record.stage = stage
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_extra_fields(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exception"] = record.exc_text
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        extras = " ".join(f"{k}={v}" for k, v in _extra_fields(record).items() if v is not None)
        line = f"{self.formatTime(record)} {record.levelname:<8} {record.name}: "
        line += record.getMessage() + (f" | {extras}" if extras else "")
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        elif record.exc_text:
            line += "\n" + record.exc_text
        return line


def configure_logging(
    level: str = "INFO", fmt: str = "json", secret_values: Iterable[str] = ()
) -> None:
    """Idempotently configure the root logger. Safe to call more than once."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else ConsoleFormatter())
    handler.addFilter(RequestIdFilter())
    handler.addFilter(RedactionFilter(secret_values))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # Route uvicorn through the same handler/format.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers = []
        uv_logger.propagate = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
