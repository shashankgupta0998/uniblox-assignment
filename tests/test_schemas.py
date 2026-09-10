"""B2 gate — Pydantic schemas.  Protects: I14 · [D32] [D36].

Every request model: extra="forbid", strict=True, quantities Field(gt=0, le=100), and NO money
field accepted from a client.  Every response model mirrors its TAD §2/§3 object field-for-field,
all money as int with a `_minor` suffix.
"""
from __future__ import annotations

import dataclasses
import inspect
import re
from pathlib import Path
from typing import Any, get_args, get_origin

import pytest
from pydantic import BaseModel, ValidationError

from src.api import schemas
from src.core.carts import CartLineView, CartView
from src.core.coupons import GenerationResult
from src.core.models import Cart, CartState, Coupon, CouponState, Customer, Order, OrderLine
from src.core.reports import ItemsPurchased, Report

REQUEST_MODELS = [
    cls for _, cls in inspect.getmembers(schemas, inspect.isclass)
    if issubclass(cls, BaseModel) and cls.__name__.endswith("Request")
]
RESPONSE_MODELS = [
    cls for _, cls in inspect.getmembers(schemas, inspect.isclass)
    if issubclass(cls, BaseModel) and cls.__name__.endswith("Response")
]
MONEY_WORDS = re.compile(r"minor|price|amount|discount|gross|net|total|cost", re.I)


def test_request_models_are_exactly_the_three_bodies() -> None:
    """TAD §8: only add-item, set-quantity, and checkout carry a JSON body."""
    assert sorted(m.__name__ for m in REQUEST_MODELS) == [
        "AddItemRequest", "CheckoutRequest", "SetQuantityRequest",
    ]


@pytest.mark.parametrize("model", REQUEST_MODELS, ids=lambda m: m.__name__)
def test_request_model_is_strict_and_closed(model: type[BaseModel]) -> None:
    """B2 DoD: extra="forbid" and strict=True on the EFFECTIVE config of every request model."""
    assert model.model_config.get("extra") == "forbid", model.__name__
    assert model.model_config.get("strict") is True, model.__name__


@pytest.mark.parametrize("model", REQUEST_MODELS, ids=lambda m: m.__name__)
def test_request_model_accepts_no_money(model: type[BaseModel]) -> None:
    """B2 DoD / I14: no money field is ever accepted from a client."""
    for name, field in model.model_fields.items():
        assert not name.endswith("_minor"), f"{model.__name__}.{name}"
        assert not MONEY_WORDS.search(name), f"{model.__name__}.{name} looks like money"
        assert field.annotation not in (float, int | float), f"{model.__name__}.{name}"


@pytest.mark.parametrize("model", [schemas.AddItemRequest, schemas.SetQuantityRequest], ids=lambda m: m.__name__)
def test_quantity_bounds(model: type[BaseModel]) -> None:
    """B2 DoD: quantities are Field(gt=0, le=100).  [D32]"""
    field = model.model_fields["quantity"]
    assert field.annotation is int
    constraints = {type(m).__name__: m for m in field.metadata}
    assert constraints["Gt"].gt == 0
    assert constraints["Le"].le == 100
    assert field.is_required()


def test_request_field_sets_verbatim() -> None:
    """TAD §8 bodies: {product_id, quantity}, {quantity}, {customer_id, coupon_code}."""
    assert list(schemas.AddItemRequest.model_fields) == ["product_id", "quantity"]
    assert list(schemas.SetQuantityRequest.model_fields) == ["quantity"]
    assert list(schemas.CheckoutRequest.model_fields) == ["customer_id", "coupon_code"]
    assert schemas.CheckoutRequest.model_fields["coupon_code"].default is None
    assert schemas.CheckoutRequest.model_fields["customer_id"].is_required()


@pytest.mark.parametrize(
    "payload",
    [
        {"product_id": "prd_cable", "quantity": 0},
        {"product_id": "prd_cable", "quantity": -1},
        {"product_id": "prd_cable", "quantity": 101},
        {"product_id": "prd_cable", "quantity": 2.5},
        {"product_id": "prd_cable", "quantity": "5"},
        {"product_id": "prd_cable", "quantity": True},
        {"product_id": "prd_cable", "quantity": 10**500},
        {"product_id": "prd_cable", "quantity": 1, "unit_price_minor": 1},
        {"product_id": "prd_cable", "quantity": 1, "price": 1},
        {"product_id": 123, "quantity": 1},
        {"quantity": 1},
    ],
    ids=["zero", "neg", "101", "float", "str", "bool", "huge", "money-field", "price", "int-id", "missing-pid"],
)
def test_add_item_rejects(payload: dict[str, Any]) -> None:
    """B2 DoD at runtime: strict, bounded, closed.  [D32] [D36]"""
    with pytest.raises(ValidationError):
        schemas.AddItemRequest.model_validate(payload)


def test_add_item_accepts_the_bounds() -> None:
    assert schemas.AddItemRequest.model_validate({"product_id": "p", "quantity": 1}).quantity == 1
    assert schemas.AddItemRequest.model_validate({"product_id": "p", "quantity": 100}).quantity == 100


@pytest.mark.parametrize(
    "payload",
    [
        {"customer_id": "cus_1", "idempotency_key": "k"},      # header, never body [B4]
        {"customer_id": "cus_1", "Idempotency-Key": "k"},
        {"customer_id": "cus_1", "discount_minor": 0},
        {"customer_id": "cus_1", "coupon_code": 5},
        {"customer_id": 1},
        {},
    ],
    ids=["idem-body", "idem-header-name", "money", "coupon-int", "cid-int", "missing"],
)
def test_checkout_request_rejects(payload: dict[str, Any]) -> None:
    """B2 DoD: CheckoutRequest is closed; the Idempotency-Key is a header, not a body field."""
    with pytest.raises(ValidationError):
        schemas.CheckoutRequest.model_validate(payload)


def test_checkout_request_coupon_optional() -> None:
    assert schemas.CheckoutRequest.model_validate({"customer_id": "cus_1"}).coupon_code is None
    assert schemas.CheckoutRequest.model_validate({"customer_id": "cus_1", "coupon_code": None}).coupon_code is None


# --------------------------------------------------------------------------- responses


MIRRORS: list[tuple[type[BaseModel], Any]] = [
    (schemas.CustomerResponse, Customer),
    (schemas.CartLineResponse, CartLineView),
    (schemas.CartResponse, CartView),
    (schemas.OrderLineResponse, OrderLine),
    (schemas.OrderResponse, Order),
    (schemas.CouponResponse, Coupon),
    (schemas.CouponGenerationResponse, GenerationResult),
    (schemas.ItemsPurchasedResponse, ItemsPurchased),
    (schemas.ReportResponse, Report),
]


@pytest.mark.parametrize("model,dc", MIRRORS, ids=lambda x: getattr(x, "__name__", str(x)))
def test_response_mirrors_view_object_verbatim(model: type[BaseModel], dc: Any) -> None:
    """B2 DoD: response models mirror TAD §2/§3 objects exactly — same field names, same order."""
    assert list(model.model_fields) == [f.name for f in dataclasses.fields(dc)], model.__name__
    assert model.model_config.get("from_attributes") is True


def test_product_response_covers_ac_p1_and_i1() -> None:
    """TAD §8 / PRD AC-P1: id, name, unit_price_minor, available (+ the I1 counters)."""
    fields = list(schemas.ProductResponse.model_fields)
    for required in ("id", "name", "unit_price_minor", "available"):
        assert required in fields
    assert {"stock_total", "reserved", "sold"} <= set(fields)


def _flatten_annotations(model: type[BaseModel]) -> dict[str, Any]:
    return {name: f.annotation for name, f in model.model_fields.items()}


@pytest.mark.parametrize("model", RESPONSE_MODELS, ids=lambda m: m.__name__)
def test_response_money_is_int_with_minor_suffix(model: type[BaseModel]) -> None:
    """B2 DoD / I14: all money as int with `_minor` suffix; no float anywhere."""
    for name, ann in _flatten_annotations(model).items():
        assert ann is not float and float not in get_args(ann), f"{model.__name__}.{name} is float"
        if name.endswith("_minor"):
            assert ann is int, f"{model.__name__}.{name} must be int, got {ann}"
        # An int field whose name smells of money must carry the _minor suffix (bools like
        # price_changed / has_price_changes and counts like stock_total are not money).
        if ann is int and MONEY_WORDS.search(name) and name not in ("stock_total", "discount_percent"):
            assert name.endswith("_minor"), f"{model.__name__}.{name}: money without _minor"


def test_order_state_literal_matches_core() -> None:
    """TAD §2: Order.state is Literal["PLACED"] — the response says the same thing."""
    ann = schemas.OrderResponse.model_fields["state"].annotation
    assert get_args(ann) == ("PLACED",)
    core_ann = Order.__dataclass_fields__["state"].type
    assert "PLACED" in str(core_ann)


def test_no_float_token_in_module() -> None:
    """I14: the token `float` does not appear in schemas.py at all."""
    text = (Path(__file__).resolve().parent.parent / "src" / "api" / "schemas.py").read_text()
    assert not re.search(r"\bfloat\b", text)
    assert not re.search(r"\bDecimal\b", text)


def test_error_envelope_schema_matches_house_shape() -> None:
    """[D29] documented envelope: {"error": {code, message, details}}."""
    assert list(schemas.ErrorEnvelope.model_fields) == ["error"]
    assert list(schemas.ErrorBody.model_fields) == ["code", "message", "details"]


def test_responses_round_trip_from_core_objects() -> None:
    """from_attributes: a real core object serialises with identical field values and int money."""
    line = CartLineView("prd_cable", "USB-C Cable", 2, 39_900, 79_800, 39_900, False)
    view = CartView("crt_1", CartState.OPEN, (line,), 79_800, False)
    out = schemas.CartResponse.model_validate(view).model_dump(mode="json")
    assert out == {
        "id": "crt_1", "state": "OPEN", "gross_minor": 79_800, "has_price_changes": False,
        "lines": [{"product_id": "prd_cable", "product_name": "USB-C Cable", "quantity": 2,
                   "unit_price_minor": 39_900, "line_total_minor": 79_800,
                   "current_unit_price_minor": 39_900, "price_changed": False}],
    }
    assert type(out["gross_minor"]) is int

    oline = OrderLine("prd_cable", "USB-C Cable", 2, 39_900, 79_800)
    order = Order("ord_1", 1, "crt_1", "cus_1", (oline,), 79_800, 7_980, 71_820, "CPN", 10, "PLACED")
    o = schemas.OrderResponse.model_validate(order).model_dump(mode="json")
    assert (o["gross_minor"], o["discount_minor"], o["net_minor"], o["state"]) == (79_800, 7_980, 71_820, "PLACED")
    assert o["gross_minor"] - o["discount_minor"] == o["net_minor"]  # I12

    coupon = Coupon("CPN", 10, 1, "cus_1", CouponState.AVAILABLE, None)
    g = schemas.CouponGenerationResponse.model_validate(GenerationResult(coupon, 0)).model_dump(mode="json")
    assert g == {"coupon": {"code": "CPN", "percent": 10, "milestone": 1, "owner_customer_id": "cus_1",
                            "state": "AVAILABLE", "redeemed_by_order_id": None}, "pending_milestones": 0}

    report = Report(1, (ItemsPurchased("prd_cable", "USB-C Cable", 2),), 79_800, 7_980, 71_820, 1, 1, 0, 0, 5, 10)
    r = schemas.ReportResponse.model_validate(report).model_dump(mode="json")
    assert r["items_purchased"] == [{"product_id": "prd_cable", "name": "USB-C Cable", "quantity": 2}]
    assert r["coupons_generated"] == r["coupons_available"] + r["coupons_reserved"] + r["coupons_redeemed"]  # I15
