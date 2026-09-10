"""Pydantic request and response models. [TAD §3] [TAD §7] [TAD §8] [D32] [D36]

Request models: strict, closed, and money-free. A client never supplies a price; every money value
is computed server-side from the snapshot. [I14]

Response models: mirror the core view objects field for field. Every money field is `int` minor
units with a `_minor` suffix. Built from dataclasses via `from_attributes`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.core.errors import ErrorCode
from src.core.models import CartState, CouponState

# --------------------------------------------------------------------------- requests


class _RequestModel(BaseModel):
    """Every client body: unknown fields rejected, no type coercion. [D32] [D36]"""

    model_config = ConfigDict(extra="forbid", strict=True)


class AddItemRequest(_RequestModel):
    """POST /carts/{cart_id}/items"""

    product_id: str
    quantity: int = Field(gt=0, le=100)


class SetQuantityRequest(_RequestModel):
    """PUT /carts/{cart_id}/items/{product_id}"""

    quantity: int = Field(gt=0, le=100)


class CheckoutRequest(_RequestModel):
    """POST /carts/{cart_id}/checkout. The Idempotency-Key travels in the header, not the body."""

    customer_id: str
    coupon_code: str | None = None


# --------------------------------------------------------------------------- responses


class _ResponseModel(BaseModel):
    """Every response body is built from a core dataclass or view object."""

    model_config = ConfigDict(from_attributes=True)


class ProductResponse(_ResponseModel):
    """GET /products. AC-P1 fields plus the three counters so I1 is visible to a client."""

    id: str
    name: str
    unit_price_minor: int
    stock_total: int
    available: int
    reserved: int
    sold: int


class CustomerResponse(_ResponseModel):
    """GET /customers"""

    id: str
    name: str


class CartLineResponse(_ResponseModel):
    """Mirrors core.carts.CartLineView."""

    product_id: str
    product_name: str
    quantity: int
    unit_price_minor: int
    line_total_minor: int
    current_unit_price_minor: int
    price_changed: bool


class CartResponse(_ResponseModel):
    """Mirrors core.carts.CartView."""

    id: str
    state: CartState
    lines: list[CartLineResponse]
    gross_minor: int
    has_price_changes: bool


class OrderLineResponse(_ResponseModel):
    """Mirrors core.models.OrderLine."""

    product_id: str
    product_name: str
    quantity: int
    unit_price_minor: int
    line_total_minor: int


class OrderResponse(_ResponseModel):
    """Mirrors core.models.Order. [I10]"""

    id: str
    sequence: int
    cart_id: str
    customer_id: str
    lines: list[OrderLineResponse]
    gross_minor: int
    discount_minor: int
    net_minor: int
    coupon_code: str | None
    discount_percent: int | None
    state: Literal["PLACED"]


class CouponResponse(_ResponseModel):
    """Mirrors core.models.Coupon."""

    code: str
    percent: int
    milestone: int
    owner_customer_id: str
    state: CouponState
    redeemed_by_order_id: str | None


class CouponGenerationResponse(_ResponseModel):
    """Mirrors core.coupons.GenerationResult. POST /admin/coupons"""

    coupon: CouponResponse
    pending_milestones: int


class ItemsPurchasedResponse(_ResponseModel):
    """Mirrors core.reports.ItemsPurchased."""

    product_id: str
    name: str
    quantity: int


class ReportResponse(_ResponseModel):
    """Mirrors core.reports.Report. GET /admin/report"""

    orders_placed: int
    items_purchased: list[ItemsPurchasedResponse]
    gross_minor: int
    discount_minor: int
    net_minor: int
    coupons_generated: int
    coupons_available: int
    coupons_reserved: int
    coupons_redeemed: int
    n: int
    x: int


# --------------------------------------------------------------------------- errors


class ErrorBody(BaseModel):
    """The inner object of every failure. [D29]"""

    code: ErrorCode
    message: str
    details: dict[str, Any]


class ErrorEnvelope(BaseModel):
    """{"error": {...}} — documented on every route so /docs shows the failure shape."""

    error: ErrorBody
