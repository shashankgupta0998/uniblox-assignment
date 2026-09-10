"""App factory and the uvicorn entry point. [TAD §1] [D27]

`create_app` builds every piece of shared state exactly once and hangs it on `app.state`. A
per-request store would silently void every invariant, so nothing here is constructed per request
and nothing is constructed in a lifespan handler (the test transport does not run lifespan).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from src.api.errors import register_error_handlers
from src.api.routers import admin, carts, orders, products
from src.config import Config, load_config
from src.core.carts import CartService
from src.core.checkout import CheckoutService
from src.core.coupons import CouponService
from src.core.idempotency import IdempotencyRegistry
from src.core.locks import LockManager
from src.core.payments import FakePaymentGateway, PaymentGateway
from src.core.reports import ReportService
from src.core.store import InMemoryStore

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"

_DESCRIPTION = """\
Carts, idempotent checkout, milestone reward coupons, and a reconciling report.

All money is integer minor units (paise). Every failure body is
`{"error": {"code", "message", "details"}}` with a stable code.
Checkout requires an `Idempotency-Key` header; a replay returns `200` with `Idempotent-Replay: true`.
Routes under `/admin` require the `X-Admin-Token` header.
"""

_TAGS = [
    {"name": "catalogue", "description": "Seeded products and customers. Unauthenticated reads."},
    {"name": "carts", "description": "Cart lifecycle and checkout. Prices are snapshotted at add time."},
    {"name": "orders", "description": "Immutable placed orders with their own price snapshot."},
    {"name": "admin", "description": "Coupon generation, coupon listing, and the report. Requires X-Admin-Token."},
]


def create_app(config: Config, *, store: InMemoryStore, payments: PaymentGateway) -> FastAPI:
    """One store, one lock manager, one idempotency registry, one of each service. [D27]"""
    locks = LockManager(store.products.keys())
    idempotency = IdempotencyRegistry()
    coupon_service = CouponService(store, locks, config)

    app = FastAPI(
        title="Checkout and Rewards Service",
        description=_DESCRIPTION,
        version="0.1.0",
        openapi_tags=_TAGS,
    )
    app.state.config = config
    app.state.store = store
    app.state.cart_service = CartService(store, locks)
    app.state.coupon_service = coupon_service
    app.state.checkout_service = CheckoutService(store, locks, idempotency, coupon_service, payments, config)
    app.state.report_service = ReportService(store, config)

    register_error_handlers(app)
    app.include_router(products.router)
    app.include_router(carts.router)
    app.include_router(orders.router)
    app.include_router(admin.router)

    if _WEB_DIR.is_dir():
        app.mount("/demo", StaticFiles(directory=_WEB_DIR, html=True), name="demo")
    return app


def _default_app() -> FastAPI:
    config = load_config()
    return create_app(
        config,
        store=InMemoryStore(config),
        payments=FakePaymentGateway(config.payment_latency_seconds),
    )


app = _default_app()
