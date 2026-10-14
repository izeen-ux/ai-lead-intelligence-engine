"""Exception -> JSON response mapping.

All error responses share one envelope::

    {"error": {"code": "...", "message": "...", "details": {...}, "request_id": "..."}}

Unexpected exceptions are logged with a traceback but return a generic 500 so internals and
secrets never leak to clients.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import AppError
from app.core.logging import get_logger, request_id_var

logger = get_logger(__name__)


def _envelope(
    code: str, message: str, status: int, details: dict[str, Any] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
                "request_id": request_id_var.get(),
            }
        },
    )


async def app_error_handler(_: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, AppError):  # pragma: no cover - registered per type
        raise exc
    log = logger.error if exc.http_status >= 500 else logger.info
    log(exc.message, extra={"error_code": exc.code, "retryable": exc.retryable})
    return _envelope(exc.code, exc.message, exc.http_status, exc.details)


async def http_error_handler(_: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, StarletteHTTPException):  # pragma: no cover - registered per type
        raise exc
    code = "not_found" if exc.status_code == 404 else "http_error"
    return _envelope(code, str(exc.detail), exc.status_code)


async def validation_error_handler(_: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):  # pragma: no cover - registered per type
        raise exc
    errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
    return _envelope("request_validation_error", "invalid request", 422, {"errors": errors})


async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error", extra={"error_type": type(exc).__name__})
    return _envelope("internal_error", "internal server error", 500)


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
