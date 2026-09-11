"""C10 — Validation and edge cases, end-to-end through the real app.  Protects: I1 I2 I14 · [D13] [D32] [D36].

Every case goes through `app_client` (src.main.create_app + a fresh store), so this file is the
"app boots" gate for B6 and the edge-validation gate thereafter.  Cases that need CartService /
CheckoutService / CouponService (A5–A7) or the admin guard (B5) are red until those land.

"Rejected at the edge" is proven by aiming the bad body at a cart id that does not exist: if
validation ran first the answer is 422 VALIDATION_FAILED; if the service ran first it would be
404 CART_NOT_FOUND.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

GHOST_CART = "crt_does_not_exist"
KEY = {"Idempotency-Key": "k-1"}


def _err(resp: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body) == {"error"} and set(body["error"]) == {"code", "message", "details"}, body
    assert body["error"]["code"] == code, body
    assert "detail" not in body
    return body["error"]


async def _cart_with(client: httpx.AsyncClient, product_id: str = "prd_cable", quantity: int = 1) -> str:
    resp = await client.post("/carts")
    assert resp.status_code == 201, resp.text
    cart_id = resp.json()["id"]
    resp = await client.post(f"/carts/{cart_id}/items", json={"product_id": product_id, "quantity": quantity})
    assert resp.status_code == 201, resp.text
    return cart_id


# --------------------------------------------------------------------------- quantity at the edge


@pytest.mark.parametrize("quantity", [0, -1, 2.5, "5", 10**500, 101], ids=["0", "-1", "2.5", "str5", "10^500", "101"])
async def test_quantity_rejected_at_the_edge_on_add(app_client: httpx.AsyncClient, quantity: Any) -> None:
    """C10 DoD / [D32] [D36] / I14: bad quantities are rejected before any service runs."""
    resp = await app_client.post(f"/carts/{GHOST_CART}/items", json={"product_id": "prd_cable", "quantity": quantity})
    _err(resp, 422, "VALIDATION_FAILED")


@pytest.mark.parametrize("quantity", [0, -1, 2.5, "5", 10**500, 101], ids=["0", "-1", "2.5", "str5", "10^500", "101"])
async def test_quantity_rejected_at_the_edge_on_set(app_client: httpx.AsyncClient, quantity: Any) -> None:
    resp = await app_client.put(f"/carts/{GHOST_CART}/items/prd_cable", json={"quantity": quantity})
    _err(resp, 422, "VALIDATION_FAILED")


async def test_quantity_bounds_are_inclusive_one_and_hundred(app_client: httpx.AsyncClient) -> None:
    """[D32] 1 and 100 pass validation (they reach the service, which 404s the ghost cart)."""
    for q in (1, 100):
        resp = await app_client.post(f"/carts/{GHOST_CART}/items", json={"product_id": "prd_cable", "quantity": q})
        assert resp.status_code != 422, resp.text
        _err(resp, 404, "CART_NOT_FOUND")


# --------------------------------------------------------------------------- unknown fields / shapes


@pytest.mark.parametrize(
    "path,payload",
    [
        (f"/carts/{GHOST_CART}/items", {"product_id": "prd_cable", "quantity": 1, "unit_price_minor": 1}),
        (f"/carts/{GHOST_CART}/items", {"product_id": "prd_cable", "quantity": 1, "note": "x"}),
        (f"/carts/{GHOST_CART}/checkout", {"customer_id": "cus_1", "discount_minor": 0}),
        (f"/carts/{GHOST_CART}/checkout", {"customer_id": "cus_1", "idempotency_key": "k"}),
    ],
    ids=["money-on-add", "unknown-on-add", "money-on-checkout", "key-in-body"],
)
async def test_unknown_request_field_rejected(app_client: httpx.AsyncClient, path: str, payload: dict[str, Any]) -> None:
    """C10 DoD / [D36]: extra="forbid" — an unknown field is a 422, never silently ignored."""
    resp = await app_client.post(path, json=payload, headers=KEY)
    _err(resp, 422, "VALIDATION_FAILED")


@pytest.mark.parametrize(
    "path,payload",
    [
        (f"/carts/{GHOST_CART}/items", {"quantity": 1}),
        (f"/carts/{GHOST_CART}/items", {"product_id": 7, "quantity": 1}),
        (f"/carts/{GHOST_CART}/checkout", {}),
        (f"/carts/{GHOST_CART}/checkout", {"customer_id": 1}),
        (f"/carts/{GHOST_CART}/checkout", {"customer_id": "cus_1", "coupon_code": 5}),
    ],
    ids=["missing-pid", "int-pid", "missing-cid", "int-cid", "int-coupon"],
)
async def test_wrong_shape_rejected(app_client: httpx.AsyncClient, path: str, payload: dict[str, Any]) -> None:
    resp = await app_client.post(path, json=payload, headers=KEY)
    _err(resp, 422, "VALIDATION_FAILED")


async def test_non_json_body_rejected(app_client: httpx.AsyncClient) -> None:
    resp = await app_client.post(f"/carts/{GHOST_CART}/items", content=b"{not json", headers={"content-type": "application/json"})
    _err(resp, 422, "VALIDATION_FAILED")


# --------------------------------------------------------------------------- not-found


async def test_unknown_cart_404(app_client: httpx.AsyncClient) -> None:
    _err(await app_client.get(f"/carts/{GHOST_CART}"), 404, "CART_NOT_FOUND")
    _err(await app_client.post(f"/carts/{GHOST_CART}/reprice"), 404, "CART_NOT_FOUND")
    _err(await app_client.delete(f"/carts/{GHOST_CART}/items/prd_cable"), 404, "CART_NOT_FOUND")


async def test_unknown_product_404(app_client: httpx.AsyncClient) -> None:
    """C10 DoD: unknown product -> 404 PRODUCT_NOT_FOUND; the cart stays empty."""
    resp = await app_client.post("/carts")
    cart_id = resp.json()["id"]
    resp = await app_client.post(f"/carts/{cart_id}/items", json={"product_id": "prd_nope", "quantity": 1})
    _err(resp, 404, "PRODUCT_NOT_FOUND")
    resp = await app_client.put(f"/carts/{cart_id}/items/prd_nope", json={"quantity": 1})
    _err(resp, 404, "PRODUCT_NOT_FOUND")
    assert (await app_client.get(f"/carts/{cart_id}")).json()["lines"] == []


async def test_unknown_order_404(app_client: httpx.AsyncClient) -> None:
    _err(await app_client.get("/orders/ord_nope"), 404, "ORDER_NOT_FOUND")


# --------------------------------------------------------------------------- checkout edges


async def test_missing_idempotency_key_400(app_client: httpx.AsyncClient) -> None:
    """C10 DoD / I3: no Idempotency-Key header -> 400 IDEMPOTENCY_KEY_REQUIRED (not a Pydantic 422)."""
    resp = await app_client.post(f"/carts/{GHOST_CART}/checkout", json={"customer_id": "cus_1"})
    _err(resp, 400, "IDEMPOTENCY_KEY_REQUIRED")


async def test_blank_idempotency_key_400(app_client: httpx.AsyncClient) -> None:
    resp = await app_client.post(f"/carts/{GHOST_CART}/checkout", json={"customer_id": "cus_1"}, headers={"Idempotency-Key": ""})
    _err(resp, 400, "IDEMPOTENCY_KEY_REQUIRED")


async def test_idempotency_key_over_128_chars_422(app_client: httpx.AsyncClient) -> None:
    """SAD §3 / I3: the Idempotency-Key header is bounded at 128 chars -> 422 VALIDATION_FAILED naming
    the header. Found by QA B15: a 200-char key was accepted."""
    resp = await app_client.post(
        f"/carts/{GHOST_CART}/checkout", json={"customer_id": "cus_1"}, headers={"Idempotency-Key": "k" * 129}
    )
    body = _err(resp, 422, "VALIDATION_FAILED")
    locs = [e["loc"] for e in body["details"]["errors"]]
    assert any("idempotency-key" in [str(part).lower() for part in loc] for loc in locs), locs


async def test_idempotency_key_exactly_128_chars_passes_header_check(app_client: httpx.AsyncClient) -> None:
    """SAD §3: 128 is the inclusive bound. The ghost cart proves the header check was passed (404, not 422)."""
    resp = await app_client.post(
        f"/carts/{GHOST_CART}/checkout", json={"customer_id": "cus_1"}, headers={"Idempotency-Key": "k" * 128}
    )
    _err(resp, 404, "CART_NOT_FOUND")


async def test_checkout_unknown_cart_404(app_client: httpx.AsyncClient) -> None:
    resp = await app_client.post(f"/carts/{GHOST_CART}/checkout", json={"customer_id": "cus_1"}, headers=KEY)
    _err(resp, 404, "CART_NOT_FOUND")


async def test_unknown_customer_422(app_client: httpx.AsyncClient, fresh_store) -> None:
    """C10 DoD: unknown customer -> 422 UNKNOWN_CUSTOMER; nothing reserved, no order.  I1"""
    cart_id = await _cart_with(app_client, "prd_headset", 1)
    resp = await app_client.post(f"/carts/{cart_id}/checkout", json={"customer_id": "cus_999"}, headers=KEY)
    _err(resp, 422, "UNKNOWN_CUSTOMER")
    assert fresh_store.reserved["prd_headset"] == 0 and fresh_store.available["prd_headset"] == 1
    assert fresh_store.orders == []
    assert (await app_client.get(f"/carts/{cart_id}")).json()["state"] == "OPEN"


async def test_empty_cart_checkout_422(app_client: httpx.AsyncClient, fresh_store) -> None:
    """C10 DoD / [D13]: empty cart -> 422 CART_EMPTY; no order, cart still OPEN."""
    cart_id = (await app_client.post("/carts")).json()["id"]
    resp = await app_client.post(f"/carts/{cart_id}/checkout", json={"customer_id": "cus_1"}, headers=KEY)
    _err(resp, 422, "CART_EMPTY")
    assert fresh_store.orders == []
    assert (await app_client.get(f"/carts/{cart_id}")).json()["state"] == "OPEN"


async def test_double_checkout_409(app_client: httpx.AsyncClient, fresh_store) -> None:
    """C10 DoD / I2: a cart is checked out at most once — second attempt with a NEW key -> 409."""
    cart_id = await _cart_with(app_client, "prd_cable", 2)
    first = await app_client.post(f"/carts/{cart_id}/checkout", json={"customer_id": "cus_1"}, headers={"Idempotency-Key": "k-first"})
    assert first.status_code == 201, first.text
    second = await app_client.post(f"/carts/{cart_id}/checkout", json={"customer_id": "cus_1"}, headers={"Idempotency-Key": "k-second"})
    _err(second, 409, "CART_ALREADY_CHECKED_OUT")
    assert len(fresh_store.orders) == 1
    assert fresh_store.sold["prd_cable"] == 2 and fresh_store.reserved["prd_cable"] == 0


async def test_mutating_checked_out_cart_409(app_client: httpx.AsyncClient) -> None:
    """I2 / [D8]: add / set / delete / reprice on a CHECKED_OUT cart -> 409 CART_ALREADY_CHECKED_OUT."""
    cart_id = await _cart_with(app_client, "prd_cable", 1)
    assert (await app_client.post(f"/carts/{cart_id}/checkout", json={"customer_id": "cus_1"}, headers=KEY)).status_code == 201
    _err(await app_client.post(f"/carts/{cart_id}/items", json={"product_id": "prd_mouse", "quantity": 1}), 409, "CART_ALREADY_CHECKED_OUT")
    _err(await app_client.put(f"/carts/{cart_id}/items/prd_cable", json={"quantity": 2}), 409, "CART_ALREADY_CHECKED_OUT")
    _err(await app_client.delete(f"/carts/{cart_id}/items/prd_cable"), 409, "CART_ALREADY_CHECKED_OUT")
    _err(await app_client.post(f"/carts/{cart_id}/reprice"), 409, "CART_ALREADY_CHECKED_OUT")


async def test_quantity_exceeds_stock_422_advisory(app_client: httpx.AsyncClient, fresh_store) -> None:
    """[D7] [D9]: add above current stock -> 422 QUANTITY_EXCEEDS_STOCK; nothing reserved (advisory)."""
    cart_id = (await app_client.post("/carts")).json()["id"]
    resp = await app_client.post(f"/carts/{cart_id}/items", json={"product_id": "prd_headset", "quantity": 2})
    _err(resp, 422, "QUANTITY_EXCEEDS_STOCK")
    assert fresh_store.reserved["prd_headset"] == 0 and fresh_store.available["prd_headset"] == 1


# --------------------------------------------------------------------------- admin guard


@pytest.mark.parametrize(
    "method,path",
    [("GET", "/admin/report"), ("POST", "/admin/coupons"), ("GET", "/admin/coupons")],
    ids=["report", "generate", "list"],
)
async def test_missing_admin_token_403(app_client: httpx.AsyncClient, method: str, path: str) -> None:
    """C10 DoD / [D28]: missing X-Admin-Token -> 403 FORBIDDEN."""
    _err(await app_client.request(method, path), 403, "FORBIDDEN")


@pytest.mark.parametrize(
    "method,path",
    [("GET", "/admin/report"), ("POST", "/admin/coupons"), ("GET", "/admin/coupons")],
    ids=["report", "generate", "list"],
)
async def test_wrong_admin_token_403(app_client: httpx.AsyncClient, method: str, path: str) -> None:
    _err(await app_client.request(method, path, headers={"X-Admin-Token": "not-the-token"}), 403, "FORBIDDEN")


async def test_correct_admin_token_passes_the_guard(app_client: httpx.AsyncClient, admin_headers: dict[str, str]) -> None:
    """The guard lets the right token through: report is 200 on a fresh store."""
    resp = await app_client.get("/admin/report", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["orders_placed"] == 0
