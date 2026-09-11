"""A2 gate (errors half) — one DomainError subclass per ErrorCode, each with a FIXED http_status
from the TAD §7 catalogue.  One code maps to exactly one status.  Protects: the error contract [D29].
"""
from __future__ import annotations

import re

import pytest

from src.core.errors import DomainError, ErrorCode

# TAD §7 — the catalogue, verbatim.
CATALOGUE: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_FAILED: 422,
    ErrorCode.CART_NOT_FOUND: 404,
    ErrorCode.CART_EMPTY: 422,
    ErrorCode.CART_ALREADY_CHECKED_OUT: 409,
    ErrorCode.CART_MODIFIED: 409,
    ErrorCode.PRODUCT_NOT_FOUND: 404,
    ErrorCode.QUANTITY_EXCEEDS_STOCK: 422,
    ErrorCode.INSUFFICIENT_INVENTORY: 409,
    ErrorCode.PRICE_CHANGED: 409,
    ErrorCode.UNKNOWN_CUSTOMER: 422,
    ErrorCode.COUPON_INVALID: 422,
    ErrorCode.COUPON_ALREADY_REDEEMED: 422,
    ErrorCode.COUPON_IN_USE: 409,
    ErrorCode.NO_ELIGIBLE_MILESTONE: 409,
    ErrorCode.IDEMPOTENCY_KEY_REQUIRED: 400,
    ErrorCode.IDEMPOTENCY_KEY_REUSED: 409,
    ErrorCode.REQUEST_IN_PROGRESS: 409,
    ErrorCode.PAYMENT_DECLINED: 402,
    ErrorCode.ORDER_NOT_FOUND: 404,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.INTERNAL_ERROR: 500,
}


def _all_subclasses(cls: type) -> list[type]:
    out: list[type] = []
    for sub in cls.__subclasses__():
        out.append(sub)
        out.extend(_all_subclasses(sub))
    return out


def _in_errors_module(cls: type) -> bool:
    """Only classes defined in src.core.errors count — test-local probe subclasses do not."""
    return cls.__module__ == "src.core.errors"


def _concrete() -> list[type[DomainError]]:
    return [
        c for c in _all_subclasses(DomainError)
        if _in_errors_module(c) and isinstance(c.__dict__.get("code"), ErrorCode)
    ]


def _camel(code: ErrorCode) -> str:
    return "".join(part.capitalize() for part in code.value.split("_"))


def test_catalogue_table_matches_enum() -> None:
    """Guard on the test itself: the table above covers every ErrorCode member exactly once."""
    assert set(CATALOGUE) == set(ErrorCode)


@pytest.mark.parametrize("code", list(ErrorCode), ids=lambda c: c.value)
def test_exactly_one_subclass_per_code(code: ErrorCode) -> None:
    """A2 DoD: one DomainError subclass per ErrorCode, with a fixed http_status."""
    matches = [c for c in _concrete() if c.code is code]
    assert len(matches) == 1, f"{code.value}: expected exactly one subclass, found {[m.__name__ for m in matches]}"
    cls = matches[0]
    assert cls.__dict__.get("http_status") == CATALOGUE[code], (
        f"{cls.__name__}: http_status {cls.__dict__.get('http_status')} != TAD §7 {CATALOGUE[code]}"
    )
    assert cls.__name__ == _camel(code), f"{cls.__name__} should be named {_camel(code)}"


def test_one_code_maps_to_exactly_one_status() -> None:
    """TAD §7: one code ⇒ exactly one status, across every subclass."""
    seen: dict[ErrorCode, set[int]] = {}
    for cls in _concrete():
        seen.setdefault(cls.code, set()).add(cls.http_status)
    assert all(len(statuses) == 1 for statuses in seen.values()), seen
    assert {c: next(iter(s)) for c, s in seen.items()} == CATALOGUE


def test_no_stray_subclasses() -> None:
    """No DomainError subclass exists without a code from the catalogue (no invented codes)."""
    for cls in filter(_in_errors_module, _all_subclasses(DomainError)):
        code = getattr(cls, "code", None)
        assert isinstance(code, ErrorCode), f"{cls.__name__} has no ErrorCode"
        assert code in CATALOGUE


@pytest.mark.parametrize("code", list(ErrorCode), ids=lambda c: c.value)
def test_instance_carries_code_status_message_details(code: ErrorCode) -> None:
    """[D29]: the exception carries everything the HTTP layer needs, generically."""
    cls = next(c for c in _concrete() if c.code is code)
    exc = cls("boom")
    assert exc.code is code and exc.http_status == CATALOGUE[code]
    assert exc.message == "boom" and exc.details == {}
    assert str(exc) == "boom"
    exc2 = cls("x", {"k": 1})
    assert exc2.details == {"k": 1}
    assert isinstance(exc2, DomainError) and isinstance(exc2, Exception)


def test_statuses_are_from_the_allowed_set() -> None:
    assert {cls.http_status for cls in _concrete()} == {400, 402, 403, 404, 409, 422, 500}


def test_only_domain_errors_are_raised_from_core() -> None:
    """TAD §7 / [D29]: src/core raises DomainError subclasses only — no bare HTTP exceptions."""
    from pathlib import Path

    core = Path(__file__).resolve().parent.parent.parent / "src" / "core"
    for py in core.glob("*.py"):
        assert not re.search(r"HTTPException", py.read_text()), py.name
