"""Dependency accessors. [TAD §1]

The app factory (B6) builds every singleton once and attaches it to `app.state`. Handlers reach
them through these accessors, never through module globals, so each test app owns its own store
and nothing leaks between tests. A per-request store would silently void every invariant. [D27]

Attributes the factory must set on `app.state`:
    config            Config
    store             InMemoryStore
    cart_service      CartService
    coupon_service    CouponService
    checkout_service  CheckoutService
    report_service    ReportService
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Header, Request

from src.config import Config
from src.core.carts import CartService
from src.core.checkout import CheckoutService
from src.core.coupons import CouponService
from src.core.errors import Forbidden
from src.core.reports import ReportService
from src.core.store import InMemoryStore


def get_config(request: Request) -> Config:
    return request.app.state.config


def get_store(request: Request) -> InMemoryStore:
    return request.app.state.store


def get_cart_service(request: Request) -> CartService:
    return request.app.state.cart_service


def get_coupon_service(request: Request) -> CouponService:
    return request.app.state.coupon_service


def get_checkout_service(request: Request) -> CheckoutService:
    return request.app.state.checkout_service


def get_report_service(request: Request) -> ReportService:
    return request.app.state.report_service


def require_admin(
    request: Request,
    x_admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
) -> None:
    """Router-level guard for /admin/*. Missing, empty, or wrong token -> 403 FORBIDDEN. [D28]

    The header is optional at the FastAPI layer so a missing token is our 403, not Pydantic's 422.
    Runs before the route body, so admin routes are guarded even while the services are stubs.
    """
    expected = get_config(request).admin_token
    if x_admin_token is None or x_admin_token == "":
        raise Forbidden("A valid X-Admin-Token header is required.")
    # Compare bytes: compare_digest on str raises TypeError for non-ASCII input, and a wrong token
    # of any byte sequence must be a plain mismatch (403), never a 500. Constant-time either way.
    if not secrets.compare_digest(x_admin_token.encode("utf-8"), expected.encode("utf-8")):
        raise Forbidden("A valid X-Admin-Token header is required.")
