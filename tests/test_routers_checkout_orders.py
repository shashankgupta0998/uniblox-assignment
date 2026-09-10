"""B4 gate — checkout route and orders router.  Protects: I3 · [D21] [D23].

Idempotency-Key required -> 400 IDEMPOTENCY_KEY_REQUIRED before anything else is touched.
CheckoutResult.replayed -> 200 + `Idempotent-Replay: true`; fresh -> 201 with no such header.
The route passes the key straight through and never computes the fingerprint or touches the
idempotency registry.  Mounted on a throwaway app with a recording fake CheckoutService, so it
depends on neither A7 nor B6.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from src.api.errors import register_error_handlers
from src.api.routers import carts as carts_router
from src.api.routers import orders as orders_router
from src.config import Config
from src.core.checkout import CheckoutResult
from src.core.errors import CartEmpty, CartNotFound, InsufficientInventory, PaymentDeclined, RequestInProgress
from src.core.models import Order, OrderLine
from src.core.store import InMemoryStore

SRC = Path(__file__).resolve().parent.parent / "src"

ORDER = Order(
    id="ord_1", sequence=1, cart_id="crt_1", customer_id="cus_1",
    lines=(OrderLine("prd_cable", "USB-C Cable", 2, 39_900, 79_800),),
    gross_minor=79_800, discount_minor=7_980, net_minor=71_820,
    coupon_code="CPN", discount_percent=10, state="PLACED",
)
BODY = {"customer_id": "cus_1", "coupon_code": "CPN"}


class RecordingCheckoutService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.replayed = False
        self.raise_next: Exception | None = None

    async def checkout(self, **kwargs: Any) -> CheckoutResult:
        self.calls.append(kwargs)
        if self.raise_next is not None:
            exc, self.raise_next = self.raise_next, None
            raise exc
        return CheckoutResult(order=ORDER, replayed=self.replayed)


class ExplodingState:
    """app.state stand-in whose checkout_service access raises — proves the header check comes first."""

    def __init__(self, store: InMemoryStore) -> None:
        self.store = store
        self.config = Config()

    @property
    def checkout_service(self) -> Any:
        raise AssertionError("checkout_service was resolved before the Idempotency-Key check")


def _app(svc: Any, store: InMemoryStore | None = None) -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(carts_router.router)
    app.include_router(orders_router.router)
    store = store or InMemoryStore(Config())
    app.state.config = Config()
    app.state.store = store
    app.state.checkout_service = svc
    return app


@pytest.fixture
def svc() -> RecordingCheckoutService:
    return RecordingCheckoutService()


@pytest.fixture
async def client(svc: RecordingCheckoutService):
    store = InMemoryStore(Config())
    store.orders_by_id["ord_1"] = ORDER
    store.orders.append(ORDER)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(svc, store)), base_url="http://t") as c:
        yield c


# --------------------------------------------------------------------------- structure


def test_routes_and_status_codes() -> None:
    table = {(m, r.path, r.status_code or 200) for r in carts_router.router.routes + orders_router.router.routes
             if isinstance(r, APIRoute) for m in r.methods}
    assert ("POST", "/carts/{cart_id}/checkout", 201) in table
    assert ("GET", "/orders/{order_id}", 200) in table


def test_handlers_are_async_def() -> None:
    """[D26]"""
    for r in carts_router.router.routes + orders_router.router.routes:
        assert inspect.iscoroutinefunction(r.endpoint), r.path


def test_api_layer_never_fingerprints_or_touches_the_registry() -> None:
    """B4 DoD: the fingerprint lives in CheckoutService; the route never computes it."""
    for py in list((SRC / "api").rglob("*.py")):
        text = py.read_text()
        assert not re.search(r"fingerprint|hashlib|sha256|IdempotencyRegistry|\.claim\(|\.release\(|\.complete\(", text), py.name


def test_checkout_route_calls_service_with_the_frozen_keyword_signature() -> None:
    """TAD §3.9: checkout(*, cart_id, customer_id, coupon_code, idempotency_key) — keyword-only."""
    text = (SRC / "api" / "routers" / "carts.py").read_text()
    assert re.search(r"\.checkout\(\s*cart_id=", text)
    for kw in ("cart_id=", "customer_id=body.customer_id", "coupon_code=body.coupon_code", "idempotency_key="):
        assert kw in text, kw


# --------------------------------------------------------------------------- header precedence


async def test_missing_key_is_400_not_422(client: httpx.AsyncClient, svc: RecordingCheckoutService) -> None:
    """B4 DoD: absent Idempotency-Key -> 400 IDEMPOTENCY_KEY_REQUIRED; service never called; no replay header."""
    resp = await client.post("/carts/crt_1/checkout", json=BODY)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert "Idempotent-Replay" not in resp.headers
    assert svc.calls == []


@pytest.mark.parametrize("value", ["", "   ", "\t"])
async def test_blank_key_is_400(client: httpx.AsyncClient, svc: RecordingCheckoutService, value: str) -> None:
    resp = await client.post("/carts/crt_1/checkout", json=BODY, headers={"Idempotency-Key": value})
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert svc.calls == []


async def test_header_check_precedes_service_resolution() -> None:
    """B4 point: the service is not even looked up before the header check."""
    app = _app(None)
    app.state = ExplodingState(InMemoryStore(Config()))  # type: ignore[assignment]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/carts/crt_1/checkout", json=BODY)
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


async def test_key_header_is_case_insensitive(client: httpx.AsyncClient, svc: RecordingCheckoutService) -> None:
    resp = await client.post("/carts/crt_1/checkout", json=BODY, headers={"idempotency-key": "k-lower"})
    assert resp.status_code == 201, resp.text
    assert svc.calls[-1]["idempotency_key"] == "k-lower"


# --------------------------------------------------------------------------- pass-through and replay


async def test_fresh_checkout_201_no_replay_header_key_passed_through(client: httpx.AsyncClient, svc: RecordingCheckoutService) -> None:
    """B4 DoD: fresh -> 201, exactly the four frozen kwargs, key verbatim, no Idempotent-Replay header."""
    resp = await client.post("/carts/crt_1/checkout", json=BODY, headers={"Idempotency-Key": "  k-abc  "})
    assert resp.status_code == 201, resp.text
    assert "Idempotent-Replay" not in resp.headers
    assert "idempotent-replay" not in {k.lower() for k in resp.headers}
    assert svc.calls == [{"cart_id": "crt_1", "customer_id": "cus_1", "coupon_code": "CPN", "idempotency_key": "  k-abc  "}]
    body = resp.json()
    assert body["id"] == "ord_1" and body["state"] == "PLACED"
    assert (body["gross_minor"], body["discount_minor"], body["net_minor"]) == (79_800, 7_980, 71_820)
    assert body["gross_minor"] - body["discount_minor"] == body["net_minor"]  # I12
    assert body["lines"][0]["line_total_minor"] == 79_800


async def test_coupon_code_none_passes_through_as_none(client: httpx.AsyncClient, svc: RecordingCheckoutService) -> None:
    resp = await client.post("/carts/crt_1/checkout", json={"customer_id": "cus_2"}, headers={"Idempotency-Key": "k"})
    assert resp.status_code == 201
    assert svc.calls == [{"cart_id": "crt_1", "customer_id": "cus_2", "coupon_code": None, "idempotency_key": "k"}]


async def test_replay_200_with_exact_header_and_identical_body(client: httpx.AsyncClient, svc: RecordingCheckoutService) -> None:
    """B4 DoD / [D23]: replayed -> 200 + `Idempotent-Replay: true` (exact string), byte-identical body."""
    fresh = await client.post("/carts/crt_1/checkout", json=BODY, headers={"Idempotency-Key": "k"})
    svc.replayed = True
    replay = await client.post("/carts/crt_1/checkout", json=BODY, headers={"Idempotency-Key": "k"})
    assert fresh.status_code == 201 and replay.status_code == 200
    assert replay.headers["Idempotent-Replay"] == "true"
    assert replay.content == fresh.content


@pytest.mark.parametrize(
    "exc,status,code",
    [
        (CartNotFound("x"), 404, "CART_NOT_FOUND"),
        (CartEmpty("x"), 422, "CART_EMPTY"),
        (InsufficientInventory("x"), 409, "INSUFFICIENT_INVENTORY"),
        (RequestInProgress("x"), 409, "REQUEST_IN_PROGRESS"),
        (PaymentDeclined("x"), 402, "PAYMENT_DECLINED"),
    ],
)
async def test_service_errors_map_generically(client: httpx.AsyncClient, svc: RecordingCheckoutService, exc: Exception, status: int, code: str) -> None:
    svc.raise_next = exc
    resp = await client.post("/carts/crt_1/checkout", json=BODY, headers={"Idempotency-Key": "k"})
    assert resp.status_code == status and resp.json()["error"]["code"] == code
    assert "Idempotent-Replay" not in resp.headers


async def test_bad_checkout_body_422_service_not_called(client: httpx.AsyncClient, svc: RecordingCheckoutService) -> None:
    for payload in ({}, {"customer_id": "cus_1", "extra": 1}, {"customer_id": "cus_1", "idempotency_key": "k"}):
        resp = await client.post("/carts/crt_1/checkout", json=payload, headers={"Idempotency-Key": "k"})
        assert resp.status_code == 422 and resp.json()["error"]["code"] == "VALIDATION_FAILED", payload
    assert svc.calls == []


# --------------------------------------------------------------------------- orders


async def test_get_order_200_snapshot(client: httpx.AsyncClient) -> None:
    """I10: the order is returned exactly as stored."""
    resp = await client.get("/orders/ord_1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "ord_1" and body["coupon_code"] == "CPN" and body["discount_percent"] == 10
    assert set(body) == {"id", "sequence", "cart_id", "customer_id", "lines", "gross_minor", "discount_minor",
                         "net_minor", "coupon_code", "discount_percent", "state"}


async def test_get_order_404(client: httpx.AsyncClient) -> None:
    resp = await client.get("/orders/ord_nope")
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "ORDER_NOT_FOUND"
