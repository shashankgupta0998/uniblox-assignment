"""B6 gate — the app factory.  Protects: every invariant (shared state must actually be shared) · [D27].

create_app(config, *, store, payments) builds ONE store, ONE LockManager, ONE IdempotencyRegistry
and the services as singletons; the store passed in is the one wired everywhere; OpenAPI metadata
is set; web/ is mounted at /demo only if it exists; `uvicorn src.main:app` has an importable app.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from src.config import Config
from src.core.checkout import CheckoutService
from src.core.idempotency import IdempotencyRegistry
from src.core.locks import LockManager
from src.core.payments import FakePaymentGateway
from src.core.store import InMemoryStore
from src.main import app as module_app
from src.main import create_app

ROOT = Path(__file__).resolve().parent.parent.parent


def _mk(store: InMemoryStore | None = None) -> tuple[FastAPI, InMemoryStore]:
    cfg = Config()
    store = store or InMemoryStore(cfg)
    return create_app(cfg, store=store, payments=FakePaymentGateway(latency_seconds=0.0)), store


def test_create_app_seam_signature() -> None:
    """Harness contract: create_app(config: Config, *, store: InMemoryStore, payments: PaymentGateway) -> FastAPI."""
    sig = inspect.signature(create_app)
    params = list(sig.parameters.values())
    assert [p.name for p in params] == ["config", "store", "payments"]
    assert params[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert params[1].kind is inspect.Parameter.KEYWORD_ONLY and params[2].kind is inspect.Parameter.KEYWORD_ONLY
    assert not inspect.iscoroutinefunction(create_app)


def test_the_store_passed_in_is_the_one_wired_everywhere() -> None:
    """B6 DoD: singletons — the given store is app.state.store and is what every service holds."""
    app, store = _mk()
    st = app.state
    assert st.store is store
    assert isinstance(st.config, Config)
    # every service must be built around the SAME store / locks / registry objects
    holders = {
        "cart_service": st.cart_service, "coupon_service": st.coupon_service,
        "checkout_service": st.checkout_service, "report_service": st.report_service,
    }
    stores = {name: next(v for v in vars(svc).values() if isinstance(v, InMemoryStore)) for name, svc in holders.items()}
    assert all(s is store for s in stores.values()), stores
    locks = [v for svc in holders.values() for v in vars(svc).values() if isinstance(v, LockManager)]
    assert len(locks) == 3 and all(l is locks[0] for l in locks), "CartService, CouponService, CheckoutService must share ONE LockManager"
    regs = [v for v in vars(st.checkout_service).values() if isinstance(v, IdempotencyRegistry)]
    assert len(regs) == 1
    coupons_in_checkout = [v for v in vars(st.checkout_service).values() if v is st.coupon_service]
    assert coupons_in_checkout, "CheckoutService must hold the SAME CouponService instance as app.state"
    assert isinstance(st.checkout_service, CheckoutService)


def test_payments_gateway_passed_in_is_the_one_used() -> None:
    gw = FakePaymentGateway(latency_seconds=0.0)
    app = create_app(Config(), store=InMemoryStore(Config()), payments=gw)
    assert any(v is gw for v in vars(app.state.checkout_service).values())


async def test_two_requests_on_one_app_hit_the_same_store() -> None:
    """[D27] shared state is shared: a cart created by request 1 is visible to request 2 and in the store."""
    app, store = _mk()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r1 = await c.post("/carts")
        assert r1.status_code == 201, r1.text
        cart_id = r1.json()["id"]
        assert cart_id in store.carts
        r2 = await c.get(f"/carts/{cart_id}")
        assert r2.status_code == 200 and r2.json()["id"] == cart_id


async def test_two_apps_do_not_share_state() -> None:
    app_a, store_a = _mk()
    app_b, store_b = _mk()
    assert app_a.state.store is not app_b.state.store
    assert app_a.state.cart_service is not app_b.state.cart_service
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_a), base_url="http://a") as ca, \
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app_b), base_url="http://b") as cb:
        cart_id = (await ca.post("/carts")).json()["id"]
        assert cart_id in store_a.carts and cart_id not in store_b.carts
        assert (await cb.get(f"/carts/{cart_id}")).status_code == 404


async def test_two_apps_two_stores_visible_through_products_route() -> None:
    """[D27] B6-only proof (no CartService needed): each app answers from the store it was given."""
    app_a, store_a = _mk()
    app_b, store_b = _mk()
    store_a.products = dict(store_a.products); store_b.products = dict(store_b.products)
    for st, n in ((store_a, 1), (store_b, 0)):
        st.products.setdefault("prd_probe", None)
        st.products.pop("prd_probe")
        st.available["prd_probe"] = n; st.reserved["prd_probe"] = 0; st.sold["prd_probe"] = 0
        from src.core.models import Product
        st.products["prd_probe"] = Product("prd_probe", "Probe", 1, n)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_a), base_url="http://a") as ca, \
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app_b), base_url="http://b") as cb:
        pa = {p["id"]: p for p in (await ca.get("/products")).json()}
        pb = {p["id"]: p for p in (await cb.get("/products")).json()}
    assert pa["prd_probe"]["available"] == 1 and pb["prd_probe"]["available"] == 0
    assert (await _count(app_a)) == 1 and (await _count(app_b)) == 1  # two GETs on one app, same store


async def _count(app: FastAPI) -> int:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x") as c:
        a = (await c.get("/products")).json()
        b = (await c.get("/products")).json()
    assert a == b
    return sum(1 for p in a if p["id"] == "prd_probe")


def test_no_per_request_construction_and_no_lifespan() -> None:
    """B6 DoD: singletons are built in create_app, not per request and not in a lifespan handler."""
    text = (ROOT / "src" / "main.py").read_text()
    code = "\n".join(l.split("#", 1)[0] for l in text.splitlines())
    assert not re.search(r"@app\.on_event|lifespan\s*=", code)
    for py in (ROOT / "src" / "api").rglob("*.py"):
        assert not re.search(r"InMemoryStore\(|LockManager\(|IdempotencyRegistry\(", py.read_text()), py.name


def test_openapi_metadata_and_paths() -> None:
    """B6 DoD: OpenAPI title/description/tags set; all TAD §8 paths documented."""
    app, _ = _mk()
    spec = app.openapi()
    assert spec["info"]["title"] and spec["info"]["title"] != "FastAPI"
    assert spec["info"].get("description")
    tags = {t["name"] for t in spec.get("tags", [])}
    assert {"carts", "orders", "admin"} <= tags
    expected = {
        "/products", "/customers", "/carts", "/carts/{cart_id}", "/carts/{cart_id}/items",
        "/carts/{cart_id}/items/{product_id}", "/carts/{cart_id}/reprice", "/carts/{cart_id}/checkout",
        "/orders/{order_id}", "/admin/coupons", "/admin/report",
    }
    assert expected <= set(spec["paths"]), expected - set(spec["paths"])
    assert set(spec["paths"]["/carts/{cart_id}/checkout"]["post"]["responses"]) >= {"201", "200", "400"}


async def test_docs_served() -> None:
    app, _ = _mk()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/docs")).status_code == 200
        assert (await c.get("/openapi.json")).status_code == 200


async def test_demo_mounted_only_if_web_exists() -> None:
    app, _ = _mk()
    mounted = any(getattr(r, "path", "") == "/demo" for r in app.routes)
    assert mounted == (ROOT / "web").is_dir()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/demo/")
        assert resp.status_code == (200 if mounted else 404)


def test_module_level_app_for_uvicorn() -> None:
    """B6 DoD: `uvicorn src.main:app` has an app to run; it is built through the same factory."""
    assert isinstance(module_app, FastAPI)
    assert isinstance(module_app.state.store, InMemoryStore)
    assert isinstance(module_app.state.checkout_service, CheckoutService)


async def test_error_handlers_registered_on_factory_app() -> None:
    app, _ = _mk()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/carts/x/items", json={"quantity": 0})
        assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_FAILED"
        r = await c.get("/admin/report")
        assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"
