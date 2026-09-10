"""DomainError hierarchy and the stable ErrorCode catalogue. [TAD §3.1] [TAD §7] [D29]

One concrete subclass per code. Each fixes `code` and `http_status` as class attributes, so the
HTTP layer maps any DomainError generically and never switches on the code. One code maps to
exactly one status.
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


# --- one subclass per code; statuses from TAD §7 ---------------------------------------------


class ValidationFailed(DomainError):
    code = ErrorCode.VALIDATION_FAILED
    http_status = 422


class CartNotFound(DomainError):
    code = ErrorCode.CART_NOT_FOUND
    http_status = 404


class CartEmpty(DomainError):
    code = ErrorCode.CART_EMPTY
    http_status = 422


class CartAlreadyCheckedOut(DomainError):
    code = ErrorCode.CART_ALREADY_CHECKED_OUT
    http_status = 409


class CartModified(DomainError):
    code = ErrorCode.CART_MODIFIED
    http_status = 409


class ProductNotFound(DomainError):
    code = ErrorCode.PRODUCT_NOT_FOUND
    http_status = 404


class QuantityExceedsStock(DomainError):
    code = ErrorCode.QUANTITY_EXCEEDS_STOCK
    http_status = 422


class InsufficientInventory(DomainError):
    code = ErrorCode.INSUFFICIENT_INVENTORY
    http_status = 409


class PriceChanged(DomainError):
    code = ErrorCode.PRICE_CHANGED
    http_status = 409


class UnknownCustomer(DomainError):
    code = ErrorCode.UNKNOWN_CUSTOMER
    http_status = 422


class CouponInvalid(DomainError):
    code = ErrorCode.COUPON_INVALID
    http_status = 422


class CouponAlreadyRedeemed(DomainError):
    code = ErrorCode.COUPON_ALREADY_REDEEMED
    http_status = 422


class CouponInUse(DomainError):
    code = ErrorCode.COUPON_IN_USE
    http_status = 409


class NoEligibleMilestone(DomainError):
    code = ErrorCode.NO_ELIGIBLE_MILESTONE
    http_status = 409


class IdempotencyKeyRequired(DomainError):
    code = ErrorCode.IDEMPOTENCY_KEY_REQUIRED
    http_status = 400


class IdempotencyKeyReused(DomainError):
    code = ErrorCode.IDEMPOTENCY_KEY_REUSED
    http_status = 409


class RequestInProgress(DomainError):
    code = ErrorCode.REQUEST_IN_PROGRESS
    http_status = 409


class PaymentDeclined(DomainError):
    code = ErrorCode.PAYMENT_DECLINED
    http_status = 402


class OrderNotFound(DomainError):
    code = ErrorCode.ORDER_NOT_FOUND
    http_status = 404


class Forbidden(DomainError):
    code = ErrorCode.FORBIDDEN
    http_status = 403


class InternalError(DomainError):
    code = ErrorCode.INTERNAL_ERROR
    http_status = 500
