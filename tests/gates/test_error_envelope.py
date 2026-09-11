"""B1 gate — the error envelope.  Protects: the error contract [D29], TAD §7.

Every failure leaves the process as  {"error": {"code", "message", "details"}}.
FastAPI's default {"detail": [...]} 422 body must never appear.  No stack trace, file path, or
internal type name in any response.  The DomainError handler reads code/http_status off the
exception generically.

This file builds its own minimal FastAPI app around `register_error_handlers` so it does not
depend on B6's app factory; it is the structural gate for src/api/errors.py.
"""
from __future__ import annotations

import re
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field

from src.api.errors import register_error_handlers
from src.core.errors import DomainError, ErrorCode

ENVELOPE_KEYS = {"code", "message", "details"}
LEAK_PATTERN = re.compile(r"Traceback|File \"|\.py|src\.|pydantic|starlette|fastapi|Error\b|<class")


class _Probe(DomainError):
    """A stand-in subclass; A2 supplies the real one-per-code hierarchy."""
    code = ErrorCode.CART_NOT_FOUND
    http_status = 404


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    quantity: int = Field(gt=0, le=100)


def _app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    @app.post("/items")
    async def items(body: _Body) -> dict[str, Any]:
        return {"ok": body.quantity}

    @app.get("/domain")
    async def domain() -> None:
        raise _Probe("no such cart", {"cart_id": "crt_x"})

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret internal state /Users/somebody/src/core/checkout.py")

    return app


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


def _assert_envelope(resp: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body) == {"error"}, f"body must be exactly {{'error': ...}}, got {body}"
    err = body["error"]
    assert set(err) == ENVELOPE_KEYS, f"envelope keys must be exactly {ENVELOPE_KEYS}, got {set(err)}"
    assert err["code"] == code
    assert isinstance(err["message"], str) and err["message"]
    assert isinstance(err["details"], dict)
    assert "detail" not in body, "FastAPI default 'detail' body leaked"
    assert not LEAK_PATTERN.search(resp.text), f"internal detail leaked: {resp.text}"
    return err


@pytest.mark.parametrize(
    "payload",
    [
        {"quantity": 0},            # gt=0
        {"quantity": -1},
        {"quantity": 101},          # le=100
        {"quantity": 2.5},          # strict int
        {"quantity": "5"},          # strict int, string rejected
        {"quantity": 10**500},      # absurd size
        {"quantity": 5, "bogus": 1},  # extra="forbid"
        {},                          # missing required
    ],
    ids=["zero", "negative", "over-max", "float", "string", "huge", "unknown-field", "missing"],
)
async def test_bad_body_is_enveloped_422(client: httpx.AsyncClient, payload: dict[str, Any]) -> None:
    """B1 DoD: POST a bad body -> 422 VALIDATION_FAILED in the envelope, never Pydantic's shape."""
    resp = await client.post("/items", json=payload)
    err = _assert_envelope(resp, 422, "VALIDATION_FAILED")
    errors = err["details"]["errors"]
    assert isinstance(errors, list) and errors
    for item in errors:
        assert set(item) == {"loc", "message"}, f"only loc+message allowed, got {item}"
        for banned in ("input", "ctx", "url", "type"):
            assert banned not in item


async def test_non_json_body_is_enveloped_422(client: httpx.AsyncClient) -> None:
    """B1 DoD: a body that is not JSON at all still gets the envelope."""
    resp = await client.post("/items", content=b"not json", headers={"content-type": "application/json"})
    _assert_envelope(resp, 422, "VALIDATION_FAILED")


async def test_client_value_is_not_echoed(client: httpx.AsyncClient) -> None:
    """B1 DoD: no client input is echoed back in validation details."""
    resp = await client.post("/items", json={"quantity": "CANARY-7f3e"})
    _assert_envelope(resp, 422, "VALIDATION_FAILED")
    assert "CANARY-7f3e" not in resp.text


async def test_domain_error_uses_exception_status_and_code(client: httpx.AsyncClient) -> None:
    """B1 DoD: DomainError -> its own http_status / code / message / details, read off the exception."""
    resp = await client.get("/domain")
    err = _assert_envelope(resp, 404, "CART_NOT_FOUND")
    assert err["message"] == "no such cart"
    assert err["details"] == {"cart_id": "crt_x"}


async def test_unhandled_exception_is_500_internal_error(client: httpx.AsyncClient) -> None:
    """B1 DoD: catch-all -> 500 INTERNAL_ERROR, no trace, path, or type name."""
    resp = await client.get("/boom")
    err = _assert_envelope(resp, 500, "INTERNAL_ERROR")
    assert err["details"] == {}
    assert "secret" not in resp.text and "RuntimeError" not in resp.text


def test_domain_handler_never_switches_on_code() -> None:
    """B1 DoD: the DomainError handler is generic — no ErrorCode comparison or match in the module
    outside the two fixed constants used by the validation and catch-all handlers."""
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent.parent / "src" / "api" / "errors.py"
    text = src.read_text()
    assert not re.search(r"\bmatch\s+exc|if\s+exc\.code|==\s*ErrorCode\.|exc\.code\s*==", text)
    uses = re.findall(r"ErrorCode\.(\w+)", text)
    assert sorted(uses) == ["INTERNAL_ERROR", "VALIDATION_FAILED"], uses


def test_all_handlers_are_async() -> None:
    """[D26] every handler is async def."""
    import inspect

    from src.api import errors

    for name in ("domain_error_handler", "validation_error_handler", "unhandled_error_handler"):
        assert inspect.iscoroutinefunction(getattr(errors, name)), name
