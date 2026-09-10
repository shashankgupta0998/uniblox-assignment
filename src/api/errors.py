"""Exception handlers and envelope normalisation. [TAD §7] [D29]

Every failure leaves the process as:

    HTTP <status>   {"error": {"code": "<STABLE_CODE>", "message": "...", "details": {...}}}

Three handlers, registered by `register_error_handlers`:

- `DomainError`            -> the code and status the exception carries. Generic: never switches on code.
- `RequestValidationError` -> 422 VALIDATION_FAILED, Pydantic's default body never escapes.
- `Exception` (catch-all)  -> 500 INTERNAL_ERROR, no stack trace, path, or type name in the body.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from src.core.errors import DomainError, ErrorCode


def envelope(code: ErrorCode, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    """The one shape every error body takes. [D29]"""
    return {
        "error": {
            "code": code.value,
            "message": message,
            "details": details if details is not None else {},
        }
    }


def _error_response(status: int, code: ErrorCode, message: str, details: dict[str, Any] | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content=envelope(code, message, details))


async def domain_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Reads code / http_status / message / details off the exception. No branching on code."""
    assert isinstance(exc, DomainError)
    return _error_response(exc.http_status, exc.code, exc.message, exc.details)


def _sanitise_validation_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    """Keep only the field path and the human message. Drop `input`, `ctx`, `url`, and the
    Pydantic `type` so no client value or internal type name is echoed back."""
    cleaned: list[dict[str, Any]] = []
    for item in exc.errors():
        loc = [part for part in item.get("loc", ()) if isinstance(part, (str, int))]
        cleaned.append({"loc": loc, "message": str(item.get("msg", "invalid value"))})
    return cleaned


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """FastAPI's default 422 body is replaced by the envelope. [D29]"""
    assert isinstance(exc, RequestValidationError)
    details = {"errors": _sanitise_validation_errors(exc)}
    return _error_response(422, ErrorCode.VALIDATION_FAILED, "Request validation failed.", details)


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all. The body carries nothing about the exception: no trace, no path, no type."""
    return _error_response(500, ErrorCode.INTERNAL_ERROR, "Internal server error.")


def register_error_handlers(app: FastAPI) -> None:
    """Called once by the app factory (B6). Order is irrelevant: Starlette dispatches by type."""
    app.add_exception_handler(DomainError, domain_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
