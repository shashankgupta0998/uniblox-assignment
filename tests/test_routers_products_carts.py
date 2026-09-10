"""B3 gate — products and carts routers + deps.  Protects: I2 · [D6] [D9] [D26].

Structural: the seven cart/product routes of TAD §8 exist with the documented status codes, every
handler is `async def`, mutations delegate to CartService (never touch the store directly), and
deps read singletons off `app.state` so each app owns its own store.

Independent of B6 (app factory), A3 (store seeding) and A5 (CartService): the routers are mounted
on a throwaway app whose state is populated by hand with a recording fake CartService.
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
from src.api.routers import products as products_router
from src.config import SEED_CUSTOMERS, SEED_PRODUCTS, Config
from src.core.carts import CartLineView, CartView
from src.core.errors import CartAlreadyCheckedOut, CartNotFound, ProductNotFound
from src.core.models import CartState
from src.core.store import InMemoryStore

SRC = Path(__file__).resolve().parent.parent / "src"

# (method, path, success status, response_model expected?)
EXPECTED_ROUTES = {
    ("GET", "/products", 200),
    ("GET", "/customers", 200),
    ("POST", "/carts", 201),
    ("GET", "/carts/{cart_id}", 200),
    ("POST", "/carts/{cart_id}/items", 201),
    ("PUT", "/carts/{cart_id}/items/{product_id}", 200),
    ("DELETE", "/carts/{cart_id}/items/{product_id}", 204),
    ("POST", "/carts/{cart_id}/reprice", 200),
}
# B4 adds this to carts.py later; its presence must not fail this gate, anything else must.
ALLOWED_LATER = {("POST", "/carts/{cart_id}/checkout", 201)}


def _routes(*routers) -> set[tuple[str, str, int]]:
    out = set()
    for r in routers:
        for route in r.routes:
            assert isinstance(route, APIRoute)
            for method in route.methods:
                out.add((method, route.path, route.status_code or 200))
    return out


def test_all_seven_cart_product_routes_with_documented_status_codes() -> None:
    """B3 DoD: all routes per TAD §8 with 201 on create/add and 204 on delete."""
    table = _routes(products_router.router, carts_router.router)
    assert EXPECTED_ROUTES <= table, f"missing: {EXPECTED_ROUTES - table}"
    assert table - EXPECTED_ROUTES <= ALLOWED_LATER, f"unexpected routes: {table - EXPECTED_ROUTES - ALLOWED_LATER}"


def test_every_handler_is_async_def() -> None:
    """[D26] a def handler runs in a threadpool where asyncio.Lock protects nothing."""
    for r in (products_router.router, carts_router.router):
        for route in r.routes:
            assert inspect.iscoroutinefunction(route.endpoint), f"{route.path} {route.endpoint.__name__} is not async def"


def test_no_def_handler_in_src_api_routers() -> None:
    """[D26] belt-and-braces on the source: no plain `def` decorated as a route anywhere in routers."""
    for py in (SRC / "api" / "routers").glob("*.py"):
        text = py.read_text()
        assert not re.search(r"@router\.\w+\([^)]*\)\s*\n\s*def ", text), py.name


def test_routers_never_touch_the_store_directly_for_mutation() -> None:
    """B3 instruction: handlers delegate mutations to CartService; carts.py never references the store."""
    text = (SRC / "api" / "routers" / "carts.py").read_text()
    assert "store" not in text.replace("InMemoryStore", ""), "carts.py must not reach the store; go through CartService"
    ptext = (SRC / "api" / "routers" / "products.py").read_text()
    assert not re.search(r"store\.\w+\[[^\]]+\]\s*[-+]?=", ptext), "products.py must not assign into store dicts"


def test_deps_read_from_app_state_not_module_globals() -> None:
    """[D27] deps.py returns request.app.state.<x>; no module-level singleton exists."""
    text = (SRC / "api" / "deps.py").read_text()
    for attr in ("config", "store", "cart_service", "coupon_service", "checkout_service", "report_service"):
        assert f"request.app.state.{attr}" in text, attr
    assert not re.search(r"^(_?store|_?STORE|_?app_state)\s*=", text, re.M)
    assert "InMemoryStore(" not in text and "LockManager(" not in text


# --------------------------------------------------------------------------- throwaway app


def _view(cart_id: str = "crt_1", lines: tuple[CartLineView, ...] = ()) -> CartView:
    return CartView(cart_id, CartState.OPEN, lines, sum(l.line_total_minor for l in lines), any(l.price_changed for l in lines))


class RecordingCartService:
    """Stands in for CartService until A5; records calls and returns canned CartViews or raises."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.raise_next: Exception | None = None

    async def _record(self, name: str, *args: Any) -> CartView:
        self.calls.append((name, args))
        if self.raise_next is not None:
            exc, self.raise_next = self.raise_next, None
            raise exc
        line = CartLineView("prd_cable", "USB-C Cable", 2, 39_900, 79_800, 39_900, False)
        return _view(args[0] if args else "crt_new", (line,))

    async def create_cart(self) -> CartView:
        return await self._record("create_cart")

    async def get_cart(self, cart_id: str) -> CartView:
        return await self._record("get_cart", cart_id)

    async def add_item(self, cart_id: str, product_id: str, quantity: int) -> CartView:
        return await self._record("add_item", cart_id, product_id, quantity)

    async def set_item_quantity(self, cart_id: str, product_id: str, quantity: int) -> CartView:
        return await self._record("set_item_quantity", cart_id, product_id, quantity)

    async def remove_item(self, cart_id: str, product_id: str) -> CartView:
        return await self._record("remove_item", cart_id, product_id)

    async def reprice(self, cart_id: str) -> CartView:
        return await self._record("reprice", cart_id)


def _seeded_store(available_override: dict[str, int] | None = None) -> InMemoryStore:
    store = InMemoryStore(Config())
    # A3 seeds this in __init__; until then populate by hand so the products route has data.
    if not store.products:
        store.products = {p.id: p for p in SEED_PRODUCTS}
        store.customers = {c.id: c for c in SEED_CUSTOMERS}
        store.available = {p.id: p.stock_total for p in SEED_PRODUCTS}
        store.reserved = {p.id: 0 for p in SEED_PRODUCTS}
        store.sold = {p.id: 0 for p in SEED_PRODUCTS}
    for pid, n in (available_override or {}).items():
        store.available[pid] = n
    return store


def _app(store: InMemoryStore, carts: RecordingCartService) -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(products_router.router)
    app.include_router(carts_router.router)
    app.state.config = Config()
    app.state.store = store
    app.state.cart_service = carts
    return app


@pytest.fixture
def carts() -> RecordingCartService:
    return RecordingCartService()


@pytest.fixture
async def client(carts: RecordingCartService):
    transport = httpx.ASGITransport(app=_app(_seeded_store(), carts))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def test_get_products_lists_seed_with_counters(client: httpx.AsyncClient) -> None:
    """TAD §8 GET /products 200: id, name, unit_price_minor, available — int money.  I14"""
    resp = await client.get("/products")
    assert resp.status_code == 200
    body = resp.json()
    assert [p["id"] for p in body] == [p.id for p in SEED_PRODUCTS]
    by_id = {p["id"]: p for p in body}
    assert by_id["prd_headset"]["available"] == 1 and by_id["prd_dock"]["available"] == 3
    for p in body:
        assert set(p) == {"id", "name", "unit_price_minor", "stock_total", "available", "reserved", "sold"}
        assert type(p["unit_price_minor"]) is int
        assert p["stock_total"] == p["available"] + p["reserved"] + p["sold"]  # I1 visible to clients


async def test_get_customers(client: httpx.AsyncClient) -> None:
    resp = await client.get("/customers")
    assert resp.status_code == 200
    assert [c["id"] for c in resp.json()] == ["cus_1", "cus_2", "cus_3", "cus_4", "cus_5"]


async def test_post_carts_201_and_delegates(client: httpx.AsyncClient, carts: RecordingCartService) -> None:
    resp = await client.post("/carts")
    assert resp.status_code == 201
    assert carts.calls == [("create_cart", ())]
    body = resp.json()
    assert set(body) == {"id", "state", "lines", "gross_minor", "has_price_changes"}
    assert body["state"] == "OPEN"


async def test_get_cart_200_with_price_changed_per_line(client: httpx.AsyncClient, carts: RecordingCartService) -> None:
    resp = await client.get("/carts/crt_9")
    assert resp.status_code == 200
    assert carts.calls == [("get_cart", ("crt_9",))]
    line = resp.json()["lines"][0]
    assert set(line) == {"product_id", "product_name", "quantity", "unit_price_minor", "line_total_minor",
                         "current_unit_price_minor", "price_changed"}


async def test_post_items_201_and_passes_body_through(client: httpx.AsyncClient, carts: RecordingCartService) -> None:
    resp = await client.post("/carts/crt_1/items", json={"product_id": "prd_cable", "quantity": 2})
    assert resp.status_code == 201
    assert carts.calls == [("add_item", ("crt_1", "prd_cable", 2))]


async def test_put_item_200_absolute_quantity(client: httpx.AsyncClient, carts: RecordingCartService) -> None:
    resp = await client.put("/carts/crt_1/items/prd_cable", json={"quantity": 7})
    assert resp.status_code == 200
    assert carts.calls == [("set_item_quantity", ("crt_1", "prd_cable", 7))]


async def test_delete_item_204_empty_body(client: httpx.AsyncClient, carts: RecordingCartService) -> None:
    resp = await client.delete("/carts/crt_1/items/prd_cable")
    assert resp.status_code == 204
    assert resp.content == b""
    assert carts.calls == [("remove_item", ("crt_1", "prd_cable"))]


async def test_post_reprice_200(client: httpx.AsyncClient, carts: RecordingCartService) -> None:
    resp = await client.post("/carts/crt_1/reprice")
    assert resp.status_code == 200
    assert carts.calls == [("reprice", ("crt_1",))]


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("POST", "/carts/crt_1/items", {"product_id": "prd_cable", "quantity": 0}),
        ("POST", "/carts/crt_1/items", {"product_id": "prd_cable", "quantity": "2"}),
        ("POST", "/carts/crt_1/items", {"product_id": "prd_cable", "quantity": 2, "unit_price_minor": 1}),
        ("PUT", "/carts/crt_1/items/prd_cable", {"quantity": 101}),
        ("PUT", "/carts/crt_1/items/prd_cable", {"quantity": 1, "extra": True}),
        ("PUT", "/carts/crt_1/items/prd_cable", {}),
    ],
)
async def test_bad_bodies_rejected_at_the_edge_before_the_service(
    client: httpx.AsyncClient, carts: RecordingCartService, method: str, path: str, payload: dict[str, Any]
) -> None:
    """[D32] [D36] strict B2 models on the routes: 422 VALIDATION_FAILED, service never called."""
    resp = await client.request(method, path, json=payload)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_FAILED"
    assert carts.calls == []


@pytest.mark.parametrize(
    "exc,status,code",
    [
        (CartNotFound("no cart"), 404, "CART_NOT_FOUND"),
        (ProductNotFound("no product"), 404, "PRODUCT_NOT_FOUND"),
        (CartAlreadyCheckedOut("done"), 409, "CART_ALREADY_CHECKED_OUT"),
    ],
)
async def test_service_domain_errors_map_to_envelope(
    client: httpx.AsyncClient, carts: RecordingCartService, exc: Exception, status: int, code: str
) -> None:
    """I2 / [D9] [D29]: a DomainError raised by CartService leaves as its own status + code."""
    carts.raise_next = exc
    resp = await client.post("/carts/crt_1/items", json={"product_id": "prd_cable", "quantity": 1})
    assert resp.status_code == status
    assert resp.json()["error"]["code"] == code


async def test_two_apps_two_stores_no_shared_module_state() -> None:
    """[D27] deps read app.state: two apps built on different stores answer differently."""
    a = _app(_seeded_store({"prd_headset": 1}), RecordingCartService())
    b = _app(_seeded_store({"prd_headset": 0}), RecordingCartService())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=a), base_url="http://a") as ca, \
            httpx.AsyncClient(transport=httpx.ASGITransport(app=b), base_url="http://b") as cb:
        ha = {p["id"]: p for p in (await ca.get("/products")).json()}["prd_headset"]["available"]
        hb = {p["id"]: p for p in (await cb.get("/products")).json()}["prd_headset"]["available"]
    assert (ha, hb) == (1, 0)


async def test_unknown_route_does_not_hit_service(client: httpx.AsyncClient, carts: RecordingCartService) -> None:
    resp = await client.post("/carts/crt_1/checkout", json={"customer_id": "cus_1"})
    assert resp.status_code in (404, 405, 400, 201, 200)  # B4 adds this route; B3 must simply not crash
    assert all(name != "checkout" for name, _ in carts.calls)
