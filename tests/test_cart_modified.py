"""TAD §4.4 — the optimistic read / re-verify window: 409 CART_MODIFIED.  Protects: I1 I2 · [D24].

Checkout reads the cart's product ids WITHOUT locks, then acquires products -> ledger -> cart, then
re-reads the cart under the lock.  If the cart changed in between, it must raise CART_MODIFIED,
release everything, and let the same key retry.  The window is opened deterministically here by
holding one product lock so the checkout blocks BEFORE it reaches the cart lock, while a real PUT
for a different product (which needs only its own product lock and the cart lock) goes through.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from src.core.locks import LockManager
from src.core.store import InMemoryStore

CABLE, MOUSE = "prd_cable", "prd_mouse"
T = 0.5


def _locks_of(app) -> LockManager:
    return next(v for v in vars(app.state.cart_service).values() if isinstance(v, LockManager))


async def test_cart_modified_between_read_and_lock(app_client: httpx.AsyncClient, fresh_store: InMemoryStore) -> None:
    """TAD §4.4: a line added after the unlocked read and before the lock -> 409 CART_MODIFIED,
    nothing reserved, no order, cart OPEN, and the same key then succeeds with BOTH lines."""
    cart_id = (await app_client.post("/carts")).json()["id"]
    assert (await app_client.post(f"/carts/{cart_id}/items", json={"product_id": CABLE, "quantity": 1})).status_code == 201
    locks = _locks_of(app_client._transport.app)
    entered, go = asyncio.Event(), asyncio.Event()

    async def hold_cable() -> None:
        async with locks.acquire(product_ids=[CABLE]):
            entered.set()
            await go.wait()

    holder = asyncio.create_task(hold_cable())
    await asyncio.wait_for(entered.wait(), T)

    checkout = asyncio.create_task(
        app_client.post(f"/carts/{cart_id}/checkout", json={"customer_id": "cus_1"}, headers={"Idempotency-Key": "k"})
    )
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.sleep(0.02)
    assert not checkout.done(), "checkout should be blocked on the held product lock"

    # A real mutation of a DIFFERENT product goes through: it needs the mouse lock and the cart lock,
    # neither of which the blocked checkout holds yet.
    put = await asyncio.wait_for(
        app_client.post(f"/carts/{cart_id}/items", json={"product_id": MOUSE, "quantity": 2}), T
    )
    assert put.status_code == 201, put.text

    go.set()
    await asyncio.wait_for(holder, T)
    resp = await asyncio.wait_for(checkout, T)
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "CART_MODIFIED"
    assert all(v == 0 for v in fresh_store.reserved.values())
    assert fresh_store.orders == []
    assert (await app_client.get(f"/carts/{cart_id}")).json()["state"] == "OPEN"

    retry = await app_client.post(f"/carts/{cart_id}/checkout", json={"customer_id": "cus_1"}, headers={"Idempotency-Key": "k"})
    assert retry.status_code == 201, retry.text  # [D22] the failure released the key
    assert sorted(l["product_id"] for l in retry.json()["lines"]) == [CABLE, MOUSE]
    assert retry.json()["gross_minor"] == 39_900 + 2 * 129_900
    assert fresh_store.sold[CABLE] == 1 and fresh_store.sold[MOUSE] == 2 and fresh_store.reserved[MOUSE] == 0


async def test_quantity_change_in_the_window_is_also_cart_modified(app_client: httpx.AsyncClient, fresh_store: InMemoryStore) -> None:
    """The re-verify compares quantities too, not only the product-id set."""
    cart_id = (await app_client.post("/carts")).json()["id"]
    assert (await app_client.post(f"/carts/{cart_id}/items", json={"product_id": CABLE, "quantity": 1})).status_code == 201
    assert (await app_client.post(f"/carts/{cart_id}/items", json={"product_id": MOUSE, "quantity": 1})).status_code == 201
    locks = _locks_of(app_client._transport.app)
    entered, go = asyncio.Event(), asyncio.Event()

    async def hold_cable() -> None:
        async with locks.acquire(product_ids=[CABLE]):  # cable sorts first: checkout blocks here
            entered.set()
            await go.wait()

    holder = asyncio.create_task(hold_cable())
    await asyncio.wait_for(entered.wait(), T)
    checkout = asyncio.create_task(
        app_client.post(f"/carts/{cart_id}/checkout", json={"customer_id": "cus_1"}, headers={"Idempotency-Key": "k"})
    )
    await asyncio.sleep(0.02)
    assert not checkout.done()
    put = await asyncio.wait_for(app_client.put(f"/carts/{cart_id}/items/{MOUSE}", json={"quantity": 3}), T)
    assert put.status_code == 200, put.text
    go.set()
    await asyncio.wait_for(holder, T)
    resp = await asyncio.wait_for(checkout, T)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "CART_MODIFIED"
    assert fresh_store.orders == [] and all(v == 0 for v in fresh_store.reserved.values())
    ok = await app_client.post(f"/carts/{cart_id}/checkout", json={"customer_id": "cus_1"}, headers={"Idempotency-Key": "k"})
    assert ok.status_code == 201 and next(l for l in ok.json()["lines"] if l["product_id"] == MOUSE)["quantity"] == 3
