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

from fastapi import Request

from src.config import Config
from src.core.carts import CartService
from src.core.checkout import CheckoutService
from src.core.coupons import CouponService
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
