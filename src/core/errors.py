"""DomainError hierarchy and the stable ErrorCode catalogue. [TAD §3.1] [TAD §7] [D29]

A1: ErrorCode is complete. DomainError is the frozen base carrier. The one-subclass-per-code
hierarchy with fixed http_status values is ticket A2.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    VALIDATION_FAILED = "VALIDATION_FAILED"
    CART_NOT_FOUND = "CART_NOT_FOUND"
    CART_EMPTY = "CART_EMPTY"
    CART_ALREADY_CHECKED_OUT = "CART_ALREADY_CHECKED_OUT"
    CART_MODIFIED = "CART_MODIFIED"
    PRODUCT_NOT_FOUND = "PRODUCT_NOT_FOUND"
    QUANTITY_EXCEEDS_STOCK = "QUANTITY_EXCEEDS_STOCK"
    INSUFFICIENT_INVENTORY = "INSUFFICIENT_INVENTORY"
    PRICE_CHANGED = "PRICE_CHANGED"
    UNKNOWN_CUSTOMER = "UNKNOWN_CUSTOMER"
    COUPON_INVALID = "COUPON_INVALID"
    COUPON_ALREADY_REDEEMED = "COUPON_ALREADY_REDEEMED"
    COUPON_IN_USE = "COUPON_IN_USE"
    NO_ELIGIBLE_MILESTONE = "NO_ELIGIBLE_MILESTONE"
    IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    REQUEST_IN_PROGRESS = "REQUEST_IN_PROGRESS"
    PAYMENT_DECLINED = "PAYMENT_DECLINED"
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
    FORBIDDEN = "FORBIDDEN"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class DomainError(Exception):
    code: ErrorCode
    http_status: int
    message: str
    details: dict[str, Any]

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details if details is not None else {}
