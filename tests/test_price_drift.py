"""C7 — Price drift and reprice.  Protects: I10 · [D5] [D6].

Mutate a seeded price after add-to-cart -> checkout 409 PRICE_CHANGED with details.changed[]
naming the product and both prices; nothing reserved, nothing consumed; reprice then checkout
succeeds at the NEW price; the order records the new price and never changes afterwards.
"""
from __future__ import annotations

import dataclasses
from typing import Any

import httpx
import pytest

from src.core.models import CouponState, Order, OrderLine
from src.core.store import InMemoryStore

CABLE, MOUSE, DOCK = "prd_cable", "prd_mouse", "prd_dock"
OWNER = "cus_2"


def _set_price(store: InMemoryStore, pid: str, price: int) -> None:
    store.products[pid] = dataclasses.replace(store.products[pid], unit_price_minor=price)


def _counters(store: InMemoryStore) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    return dict(store.available), dict(store.reserved), dict(store.sold)


async def _cart_with(client: httpx.AsyncClient, *lines: tuple[str, int]) -> str:
    cart_id = (await client.post("/carts")).json()["id"]
    for pid, qty in lines:
        assert (await client.post(f"/carts/{cart_id}/items", json={"product_id": pid, "quantity": qty})).status_code == 201
    return cart_id


async def _checkout(client: httpx.AsyncClient, cart_id: str, key: str, coupon: str | None = None) -> httpx.Response:
    body: dict[str, Any] = {"customer_id": OWNER}
    if coupon:
        body["coupon_code"] = coupon
    return await client.post(f"/carts/{cart_id}/checkout", json=body, headers={"Idempotency-Key": key})


def _changed_entry(resp: httpx.Response, pid: str) -> dict[str, Any]:
    changed = resp.json()["error"]["details"]["changed"]
    assert isinstance(changed, list) and changed
    entry = next(e for e in changed if e.get("product_id") == pid)
    return entry


async def test_price_changed_409_with_details_nothing_reserved(app_client: httpx.AsyncClient, fresh_store: InMemoryStore) -> None:
    """I10 / C7 DoD [D5]: drift -> 409 PRICE_CHANGED; changed[] names the product and both prices;
    nothing reserved, nothing consumed, no order, cart OPEN."""
    cart_id = await _cart_with(app_client, (CABLE, 2))
    _set_price(fresh_store, CABLE, 45_000)
    before = _counters(fresh_store)
    r = await _checkout(app_client, cart_id, "k")
    assert r.status_code == 409 and r.json()["error"]["code"] == "PRICE_CHANGED", r.text
    entry = _changed_entry(r, CABLE)
    ints = {v for v in entry.values() if isinstance(v, int) and not isinstance(v, bool)}
    assert {39_900, 45_000} <= ints, entry  # both prices, as ints
    assert len(r.json()["error"]["details"]["changed"]) == 1
    assert _counters(fresh_store) == before and fresh_store.reserved[CABLE] == 0
    assert fresh_store.orders == []
    cart = (await app_client.get(f"/carts/{cart_id}")).json()
    assert cart["state"] == "OPEN" and cart["lines"][0]["unit_price_minor"] == 39_900 and cart["lines"][0]["price_changed"] is True
    assert cart["has_price_changes"] is True


async def test_only_drifted_lines_are_listed(app_client: httpx.AsyncClient, fresh_store: InMemoryStore) -> None:
    cart_id = await _cart_with(app_client, (CABLE, 1), (MOUSE, 1), (DOCK, 1))
    _set_price(fresh_store, MOUSE, 100_000)
    _set_price(fresh_store, DOCK, 2_600_000)
    r = await _checkout(app_client, cart_id, "k")
    assert r.status_code == 409
    changed = r.json()["error"]["details"]["changed"]
    assert sorted(e["product_id"] for e in changed) == [DOCK, MOUSE]
    assert all(v == 0 for v in fresh_store.reserved.values())


async def test_reprice_then_checkout_at_new_price(app_client: httpx.AsyncClient, fresh_store: InMemoryStore) -> None:
    """I10 / C7 DoD [D6]: reprice, then checkout (same key) -> 201 at the NEW price; the order records it."""
    cart_id = await _cart_with(app_client, (CABLE, 2), (MOUSE, 1))
    _set_price(fresh_store, CABLE, 45_000)
    assert (await _checkout(app_client, cart_id, "k")).status_code == 409
    rp = await app_client.post(f"/carts/{cart_id}/reprice")
    assert rp.status_code == 200 and rp.json()["has_price_changes"] is False
    assert next(l for l in rp.json()["lines"] if l["product_id"] == CABLE)["unit_price_minor"] == 45_000
    ok = await _checkout(app_client, cart_id, "k")  # same key: the failure did not poison it [D22]
    assert ok.status_code == 201, ok.text
    order = ok.json()
    cable = next(l for l in order["lines"] if l["product_id"] == CABLE)
    assert cable["unit_price_minor"] == 45_000 and cable["line_total_minor"] == 90_000
    assert order["gross_minor"] == 90_000 + 129_900 and order["gross_minor"] - order["discount_minor"] == order["net_minor"]
    stored = fresh_store.orders_by_id[order["id"]]
    assert next(l for l in stored.lines if l.product_id == CABLE).unit_price_minor == 45_000
    assert fresh_store.sold[CABLE] == 2 and fresh_store.reserved[CABLE] == 0
    assert (await app_client.get(f"/orders/{order['id']}")).json() == order


async def test_order_is_immutable_after_later_price_change(app_client: httpx.AsyncClient, fresh_store: InMemoryStore) -> None:
    """I10: a later product mutation never changes a past order."""
    cart_id = await _cart_with(app_client, (CABLE, 3))
    order = (await _checkout(app_client, cart_id, "k")).json()
    _set_price(fresh_store, CABLE, 1)
    again = await app_client.get(f"/orders/{order['id']}")
    assert again.json() == order
    assert again.json()["lines"][0]["unit_price_minor"] == 39_900 and again.json()["gross_minor"] == 119_700
    assert isinstance(fresh_store.orders_by_id[order["id"]], Order) and isinstance(fresh_store.orders_by_id[order["id"]].lines[0], OrderLine)


async def test_drift_with_coupon_consumes_nothing(app_client: httpx.AsyncClient, fresh_store: InMemoryStore, admin_headers, config) -> None:
    """I5 alongside I10: a PRICE_CHANGED failure leaves the coupon AVAILABLE and the key retryable."""
    for i in range(config.n):
        seq = len(fresh_store.orders) + 1
        fresh_store.append_order_locked(Order(f"ord_s{seq}", seq, f"crt_s{seq}", OWNER, (OrderLine(CABLE, "USB-C Cable", 1, 39_900, 39_900),), 39_900, 0, 39_900, None, None, "PLACED"))
    code = (await app_client.post("/admin/coupons", headers=admin_headers)).json()["coupon"]["code"]
    cart_id = await _cart_with(app_client, (DOCK, 1))
    _set_price(fresh_store, DOCK, 2_400_000)
    r = await _checkout(app_client, cart_id, "k", code)
    assert r.status_code == 409 and r.json()["error"]["code"] == "PRICE_CHANGED"
    assert fresh_store.coupons[code].state is CouponState.AVAILABLE and fresh_store.reserved[DOCK] == 0
    assert (await app_client.post(f"/carts/{cart_id}/reprice")).status_code == 200
    ok = await _checkout(app_client, cart_id, "k", code)
    assert ok.status_code == 201 and ok.json()["gross_minor"] == 2_400_000 and ok.json()["discount_minor"] == 240_000
    assert fresh_store.coupons[code].state is CouponState.REDEEMED


async def test_price_drop_is_also_a_drift(app_client: httpx.AsyncClient, fresh_store: InMemoryStore) -> None:
    """[D5] the check is snapshot != live, in either direction — a customer is never silently overcharged."""
    cart_id = await _cart_with(app_client, (MOUSE, 1))
    _set_price(fresh_store, MOUSE, 99_900)
    r = await _checkout(app_client, cart_id, "k")
    assert r.status_code == 409 and r.json()["error"]["code"] == "PRICE_CHANGED"
    entry = _changed_entry(r, MOUSE)
    assert {129_900, 99_900} <= {v for v in entry.values() if isinstance(v, int) and not isinstance(v, bool)}
