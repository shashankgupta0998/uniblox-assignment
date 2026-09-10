"""Domain dataclasses. All money is int minor units; all ids are opaque strings. [TAD §2]"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal


@dataclass(frozen=True)
class Product:
    id: str
    name: str
    unit_price_minor: int
    stock_total: int


@dataclass(frozen=True)
class Customer:
    id: str
    name: str


@dataclass
class CartItem:
    product_id: str
    quantity: int
    unit_price_minor: int  # snapshot taken at add/update time  [D5]


class CartState(str, Enum):
    OPEN = "OPEN"
    CHECKED_OUT = "CHECKED_OUT"


@dataclass
class Cart:
    id: str
    state: CartState
    items: dict[str, CartItem]  # keyed by product_id, insertion-ordered


@dataclass(frozen=True)
class OrderLine:
    product_id: str
    product_name: str  # snapshot — order must explain itself later  [I10]
    quantity: int
    unit_price_minor: int  # snapshot
    line_total_minor: int  # quantity * unit_price_minor


@dataclass(frozen=True)
class Order:
    id: str
    sequence: int  # monotonic from 1; milestone attribution  [D12]
    cart_id: str
    customer_id: str
    lines: tuple[OrderLine, ...]
    gross_minor: int
    discount_minor: int
    net_minor: int
    coupon_code: str | None
    discount_percent: int | None  # snapshot of x at order time
    state: Literal["PLACED"]  # the only terminal state; there is no cancel  [I8]


class CouponState(str, Enum):
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    REDEEMED = "REDEEMED"


@dataclass
class Coupon:
    code: str
    percent: int  # snapshot of x at generation time
    milestone: int  # k; unique across all coupons  [I7]
    owner_customer_id: str  # [D12]
    state: CouponState
    redeemed_by_order_id: str | None
