"""C3 — Concurrent oversell.  Protects: I1 · [D24].

N=20 concurrent checkouts with distinct keys against prd_headset (stock 1): exactly one 201 and
nineteen 409 INSUFFICIENT_INVENTORY; sold == 1, available == 0, reserved == 0.  A second case with
prd_dock (stock 3) asserts exactly three.  Plus the test that FAILS when the LockManager is
bypassed, so the suite proves the lock is load-bearing.

Every checkout goes end-to-end through the real app (create_app + fresh store) with a non-zero
payment latency so the in-flight window is real.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections import Counter
from typing import Any, AsyncIterator

import httpx
import pytest

from src import main as main_mod
from src.config import Config
from src.core.locks import LockManager
from src.core.payments import FakePaymentGateway
from src.core.store import InMemoryStore

HEADSET, DOCK, CABLE = "prd_headset", "prd_dock", "prd_cable"
N = 20
LATENCY = 0.05  # wide in-flight window: every racer is inside the critical section together


def _i1(store: InMemoryStore) -> None:
    for pid, p in store.products.items():
        assert p.stock_total == store.available[pid] + store.reserved[pid] + store.sold[pid], pid
        assert min(store.available[pid], store.reserved[pid], store.sold[pid]) >= 0


async def _cart_with(client: httpx.AsyncClient, product_id: str, quantity: int) -> str:
    cart_id = (await client.post("/carts")).json()["id"]
    r = await client.post(f"/carts/{cart_id}/items", json={"product_id": product_id, "quantity": quantity})
    assert r.status_code == 201, r.text
    return cart_id


async def _checkout(client: httpx.AsyncClient, cart_id: str, key: str, customer: str = "cus_1") -> httpx.Response:
    return await client.post(f"/carts/{cart_id}/checkout", json={"customer_id": customer}, headers={"Idempotency-Key": key})


async def _race(client: httpx.AsyncClient, cart_ids: list[str]) -> list[httpx.Response]:
    return await asyncio.gather(*(
        _checkout(client, cid, f"key-{i}", f"cus_{(i % 5) + 1}") for i, cid in enumerate(cart_ids)
    ))


def _histogram(responses: list[httpx.Response]) -> dict[int, int]:
    return dict(Counter(r.status_code for r in responses))


@pytest.fixture
def slow_gateway() -> FakePaymentGateway:
    return FakePaymentGateway(latency_seconds=LATENCY)


@pytest.fixture
async def racing_client(config: Config, fresh_store: InMemoryStore, slow_gateway: FakePaymentGateway, client_factory):
    async with client_factory(store=fresh_store, payments=slow_gateway) as client:
        yield client


# --------------------------------------------------------------------------- the DoD cases


async def test_stock_one_twenty_racers_exactly_one_wins(racing_client: httpx.AsyncClient, fresh_store: InMemoryStore) -> None:
    """I1 / C3 DoD: stock 1, N=20 distinct keys -> {201: 1, 409: 19}, all 409 INSUFFICIENT_INVENTORY,
    sold == 1, available == 0, reserved == 0."""
    carts = [await _cart_with(racing_client, HEADSET, 1) for _ in range(N)]
    responses = await _race(racing_client, carts)
    assert _histogram(responses) == {201: 1, 409: 19}, [r.status_code for r in responses]
    for r in responses:
        if r.status_code == 409:
            assert r.json()["error"]["code"] == "INSUFFICIENT_INVENTORY", r.text
    assert (fresh_store.sold[HEADSET], fresh_store.available[HEADSET], fresh_store.reserved[HEADSET]) == (1, 0, 0)
    assert len(fresh_store.orders) == 1
    winner = next(r for r in responses if r.status_code == 201).json()
    assert fresh_store.orders[0].id == winner["id"] and winner["lines"][0]["quantity"] == 1
    _i1(fresh_store)


async def test_stock_three_twenty_racers_exactly_three_win(racing_client: httpx.AsyncClient, fresh_store: InMemoryStore) -> None:
    """I1 / C3 DoD: stock 3, N=20 -> exactly three 201, seventeen 409 INSUFFICIENT_INVENTORY."""
    carts = [await _cart_with(racing_client, DOCK, 1) for _ in range(N)]
    responses = await _race(racing_client, carts)
    assert _histogram(responses) == {201: 3, 409: 17}, [r.status_code for r in responses]
    assert all(r.json()["error"]["code"] == "INSUFFICIENT_INVENTORY" for r in responses if r.status_code == 409)
    assert (fresh_store.sold[DOCK], fresh_store.available[DOCK], fresh_store.reserved[DOCK]) == (3, 0, 0)
    assert len(fresh_store.orders) == 3
    assert sorted(o.sequence for o in fresh_store.orders) == [1, 2, 3]
    _i1(fresh_store)


async def test_stock_three_quantity_two_exactly_one_wins_one_unit_left(racing_client: httpx.AsyncClient, fresh_store: InMemoryStore) -> None:
    """I1: stock 3, every cart wants 2 -> exactly one wins; one unit stays available, never oversold."""
    carts = [await _cart_with(racing_client, DOCK, 2) for _ in range(N)]
    responses = await _race(racing_client, carts)
    assert _histogram(responses) == {201: 1, 409: 19}
    assert (fresh_store.sold[DOCK], fresh_store.available[DOCK], fresh_store.reserved[DOCK]) == (2, 1, 0)
    _i1(fresh_store)


async def test_mixed_products_race_independently(racing_client: httpx.AsyncClient, fresh_store: InMemoryStore) -> None:
    """I1 across products: headset (1) and dock (3) carts racing together -> 1 + 3 winners."""
    carts = [await _cart_with(racing_client, HEADSET if i % 2 else DOCK, 1) for i in range(N)]
    responses = await _race(racing_client, carts)
    assert _histogram(responses) == {201: 4, 409: 16}
    assert fresh_store.sold[HEADSET] == 1 and fresh_store.sold[DOCK] == 3
    assert fresh_store.reserved == {pid: 0 for pid in fresh_store.products}
    _i1(fresh_store)


async def test_multi_line_cart_all_or_nothing(racing_client: httpx.AsyncClient, fresh_store: InMemoryStore) -> None:
    """I1 / I13: carts with {headset 1, dock 1}: exactly one wins; losers release their partial
    reservation in full — nothing stays reserved, dock's other units stay available."""
    carts = []
    for _ in range(N):
        cid = await _cart_with(racing_client, DOCK, 1)
        assert (await racing_client.post(f"/carts/{cid}/items", json={"product_id": HEADSET, "quantity": 1})).status_code == 201
        carts.append(cid)
    responses = await _race(racing_client, carts)
    assert _histogram(responses) == {201: 1, 409: 19}
    assert (fresh_store.sold[HEADSET], fresh_store.available[HEADSET], fresh_store.reserved[HEADSET]) == (1, 0, 0)
    assert (fresh_store.sold[DOCK], fresh_store.available[DOCK], fresh_store.reserved[DOCK]) == (1, 2, 0)
    _i1(fresh_store)


async def test_sequential_after_sellout_is_still_409(racing_client: httpx.AsyncClient, fresh_store: InMemoryStore) -> None:
    """I1: once sold out, a later checkout is a clean 409, and the counters do not move."""
    winner = await _cart_with(racing_client, HEADSET, 1)
    late = await _cart_with(racing_client, HEADSET, 1)  # built while stock was still 1 [D7: add reserves nothing]
    assert (await _checkout(racing_client, winner, "k-w")).status_code == 201
    r = await _checkout(racing_client, late, "k-l")
    assert r.status_code == 409 and r.json()["error"]["code"] == "INSUFFICIENT_INVENTORY"
    assert (fresh_store.sold[HEADSET], fresh_store.available[HEADSET], fresh_store.reserved[HEADSET]) == (1, 0, 0)
    _i1(fresh_store)


# --------------------------------------------------------------------------- the lock is load-bearing


class NoLockManager(LockManager):
    """A LockManager whose acquire is a no-op: every lock class is bypassed.  TEST-ONLY."""

    def acquire(self, *, product_ids=(), coupon_ledger=False, cart_id=None):  # type: ignore[override]
        @contextlib.asynccontextmanager
        async def _noop() -> AsyncIterator[None]:
            yield

        return _noop()


@pytest.fixture
def bypass_locks(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_app looks LockManager up in src.main at call time — swap it for the no-op one."""
    monkeypatch.setattr(main_mod, "LockManager", NoLockManager)


async def _same_cart_double_checkout(client: httpx.AsyncClient, store: InMemoryStore) -> tuple[dict[int, int], list[httpx.Response]]:
    cart_id = await _cart_with(client, CABLE, 1)
    responses = await asyncio.gather(_checkout(client, cart_id, "k-a"), _checkout(client, cart_id, "k-b"))
    return _histogram(list(responses)), list(responses)


async def test_with_locks_same_cart_two_keys_is_checked_out_once(racing_client: httpx.AsyncClient, fresh_store: InMemoryStore) -> None:
    """I2 with the lock: two concurrent checkouts of ONE cart (distinct keys) -> exactly one 201 and
    one 409 CART_ALREADY_CHECKED_OUT; one order; sold == 1."""
    hist, responses = await _same_cart_double_checkout(racing_client, fresh_store)
    assert hist == {201: 1, 409: 1}, hist
    assert next(r for r in responses if r.status_code == 409).json()["error"]["code"] == "CART_ALREADY_CHECKED_OUT"
    assert len(fresh_store.orders) == 1 and fresh_store.sold[CABLE] == 1
    _i1(fresh_store)


async def test_lock_is_load_bearing_bypass_breaks_i2(bypass_locks: None, config: Config, fresh_store: InMemoryStore, slow_gateway: FakePaymentGateway, client_factory) -> None:
    """C3 DoD: this test FAILS when the LockManager is bypassed... inverted: it asserts that the
    bypass DOES break the invariant, proving the lock is what holds it.  [D24]

    Without the cart lock, two concurrent checkouts of one cart both pass the OPEN check before
    either reaches the await, both reserve, both commit: two orders from one cart, the single cart
    line sold twice.  With the lock (previous test) that is impossible."""
    async with client_factory(store=fresh_store, payments=slow_gateway) as client:
        assert isinstance(client._transport.app.state.cart_service._locks, NoLockManager)  # the bypass is in effect
        hist, _ = await _same_cart_double_checkout(client, fresh_store)
    assert hist == {201: 2}, f"expected the bypass to double-checkout the cart, got {hist}"
    assert len(fresh_store.orders) == 2
    assert fresh_store.sold[CABLE] == 2, "one cart line of quantity 1 was sold twice: I2 broken without the lock"


async def test_with_locks_mutation_during_checkout_is_rejected(racing_client: httpx.AsyncClient, fresh_store: InMemoryStore) -> None:
    """I2 / I10 with the lock: a PUT arriving while the checkout is in flight waits for the cart lock
    and then sees CHECKED_OUT -> 409; the order and the cart agree."""
    cart_id = await _cart_with(racing_client, CABLE, 1)
    checkout = asyncio.create_task(_checkout(racing_client, cart_id, "k-1"))
    await asyncio.sleep(LATENCY / 4)  # checkout is now inside its await on the gateway
    put = await racing_client.put(f"/carts/{cart_id}/items/{CABLE}", json={"quantity": 50})
    order = (await checkout).json()
    assert put.status_code == 409 and put.json()["error"]["code"] == "CART_ALREADY_CHECKED_OUT"
    assert order["lines"][0]["quantity"] == 1 == fresh_store.carts[cart_id].items[CABLE].quantity
    assert fresh_store.sold[CABLE] == 1
    _i1(fresh_store)


async def test_lock_is_load_bearing_bypass_lets_cart_mutate_mid_checkout(bypass_locks: None, fresh_store: InMemoryStore, slow_gateway: FakePaymentGateway, client_factory) -> None:
    """[D24] Without the cart lock the same PUT succeeds mid-flight: the checked-out cart no longer
    matches the order that was placed from it."""
    async with client_factory(store=fresh_store, payments=slow_gateway) as client:
        cart_id = await _cart_with(client, CABLE, 1)
        checkout = asyncio.create_task(_checkout(client, cart_id, "k-1"))
        await asyncio.sleep(LATENCY / 4)
        put = await client.put(f"/carts/{cart_id}/items/{CABLE}", json={"quantity": 50})
        order = (await checkout).json()
    assert put.status_code == 200, f"expected the bypass to let the mutation through, got {put.status_code} {put.text}"
    assert order["lines"][0]["quantity"] == 1
    assert fresh_store.carts[cart_id].items[CABLE].quantity == 50, "cart drifted from its order: the lock is what prevents this"
