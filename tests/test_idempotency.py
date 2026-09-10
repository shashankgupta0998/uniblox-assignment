"""C4 — Idempotency storm.  Protects: I3 · [D19] [D20] [D21] [D22] [D23].

(a) N=10 concurrent, one key -> exactly one 201, nine 409 REQUEST_IN_PROGRESS, ONE order, inventory
    decremented once.
(b) sequential replay after completion -> 200 + `Idempotent-Replay: true`, byte-identical body.
(c) same key, different body (coupon_code / customer_id) -> 409 IDEMPOTENCY_KEY_REUSED, also while
    the original is still in flight (mismatch is checked before in-progress).
(d) retry after a FAILED checkout with the same key proceeds as a new attempt (not a cached
    failure, not KEY_REUSED) — one app, one registry, a gateway that declines once then approves.
Payment latency is non-zero throughout so the in-flight window is real.
"""
from __future__ import annotations

import asyncio
import dataclasses
from collections import Counter
from typing import Any

import httpx
import pytest

from src.core.errors import PaymentDeclined
from src.core.payments import FakePaymentGateway, PaymentResult
from src.core.store import InMemoryStore

CABLE, HEADSET = "prd_cable", "prd_headset"
LATENCY = 0.05
BODY = {"customer_id": "cus_1"}


class DeclineThenApprove:
    """Test-only PaymentGateway: declines the first `declines` charges (after the same yield), then approves."""

    def __init__(self, declines: int = 1, latency: float = LATENCY) -> None:
        self.declines, self.latency, self.calls = declines, latency, 0

    async def charge(self, *, amount_minor: int, order_ref: str) -> PaymentResult:
        self.calls += 1
        await asyncio.sleep(self.latency)
        if self.calls <= self.declines:
            raise PaymentDeclined("declined", {"order_ref": order_ref})
        return PaymentResult(reference=f"pay_{self.calls}")


def _i1(store: InMemoryStore) -> None:
    for pid, p in store.products.items():
        assert p.stock_total == store.available[pid] + store.reserved[pid] + store.sold[pid], pid


async def _cart_with(client: httpx.AsyncClient, product_id: str = CABLE, quantity: int = 1) -> str:
    cart_id = (await client.post("/carts")).json()["id"]
    r = await client.post(f"/carts/{cart_id}/items", json={"product_id": product_id, "quantity": quantity})
    assert r.status_code == 201, r.text
    return cart_id


async def _checkout(client: httpx.AsyncClient, cart_id: str, key: str, body: dict[str, Any] = BODY) -> httpx.Response:
    return await client.post(f"/carts/{cart_id}/checkout", json=body, headers={"Idempotency-Key": key})


@pytest.fixture
async def client(fresh_store: InMemoryStore, client_factory):
    async with client_factory(store=fresh_store, payments=FakePaymentGateway(latency_seconds=LATENCY)) as c:
        yield c


# --------------------------------------------------------------------------- (a) the storm


async def test_a_ten_concurrent_one_key_exactly_one_order(client: httpx.AsyncClient, fresh_store: InMemoryStore) -> None:
    """I3 / C4 (a): {201: 1, 409: 9}, every 409 REQUEST_IN_PROGRESS, one order, inventory decremented once."""
    cart_id = await _cart_with(client, CABLE, 1)
    responses = await asyncio.gather(*(_checkout(client, cart_id, "one-key") for _ in range(10)))
    hist = dict(Counter(r.status_code for r in responses))
    assert hist == {201: 1, 409: 9}, [r.status_code for r in responses]
    for r in responses:
        if r.status_code == 409:
            assert r.json()["error"]["code"] == "REQUEST_IN_PROGRESS", r.text
            assert "Idempotent-Replay" not in r.headers
    assert len(fresh_store.orders) == 1
    assert (fresh_store.sold[CABLE], fresh_store.available[CABLE], fresh_store.reserved[CABLE]) == (1, 99, 0)
    _i1(fresh_store)
    winner = next(r for r in responses if r.status_code == 201)
    assert "Idempotent-Replay" not in winner.headers
    assert winner.json()["id"] == fresh_store.orders[0].id


async def test_a_storm_then_replay_returns_the_winner(client: httpx.AsyncClient, fresh_store: InMemoryStore) -> None:
    """[D21] + [D23]: 409 REQUEST_IN_PROGRESS -> back off -> retry -> 200 replay of the ONE order."""
    cart_id = await _cart_with(client, CABLE, 2)
    responses = await asyncio.gather(*(_checkout(client, cart_id, "k") for _ in range(10)))
    winner = next(r for r in responses if r.status_code == 201)
    replay = await _checkout(client, cart_id, "k")
    assert replay.status_code == 200 and replay.headers["Idempotent-Replay"] == "true"
    assert replay.content == winner.content
    assert len(fresh_store.orders) == 1 and fresh_store.sold[CABLE] == 2


# --------------------------------------------------------------------------- (b) replay


async def test_b_sequential_replay_200_header_identical_body(client: httpx.AsyncClient, fresh_store: InMemoryStore) -> None:
    """I3 / C4 (b) [D23]: replay -> 200, Idempotent-Replay: true, byte-identical body; nothing changes."""
    cart_id = await _cart_with(client, CABLE, 3)
    first = await _checkout(client, cart_id, "k-1")
    assert first.status_code == 201 and "Idempotent-Replay" not in first.headers
    snapshot = (dict(fresh_store.sold), dict(fresh_store.available), len(fresh_store.orders))
    for _ in range(3):
        again = await _checkout(client, cart_id, "k-1")
        assert again.status_code == 200
        assert again.headers["Idempotent-Replay"] == "true"
        assert again.content == first.content
    assert (dict(fresh_store.sold), dict(fresh_store.available), len(fresh_store.orders)) == snapshot
    assert fresh_store.sold[CABLE] == 3
    _i1(fresh_store)


async def test_b_replay_body_matches_get_orders(client: httpx.AsyncClient) -> None:
    cart_id = await _cart_with(client, CABLE, 1)
    first = await _checkout(client, cart_id, "k-1")
    replay = await _checkout(client, cart_id, "k-1")
    fetched = await client.get(f"/orders/{first.json()['id']}")
    assert fetched.status_code == 200 and fetched.json() == replay.json() == first.json()


# --------------------------------------------------------------------------- (c) key reuse


@pytest.mark.parametrize(
    "second_body",
    [{"customer_id": "cus_1", "coupon_code": "NOPE"}, {"customer_id": "cus_2"}],
    ids=["different-coupon", "different-customer"],
)
async def test_c_same_key_different_body_409_key_reused(client: httpx.AsyncClient, fresh_store: InMemoryStore, second_body: dict[str, Any]) -> None:
    """I3 / C4 (c) [D20]: same key, different body -> 409 IDEMPOTENCY_KEY_REUSED — before any coupon
    or customer validation runs (the bogus coupon is never looked at)."""
    cart_id = await _cart_with(client, CABLE, 1)
    assert (await _checkout(client, cart_id, "k")).status_code == 201
    r = await _checkout(client, cart_id, "k", second_body)
    assert r.status_code == 409 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED", r.text
    assert "Idempotent-Replay" not in r.headers
    assert len(fresh_store.orders) == 1 and fresh_store.sold[CABLE] == 1


async def test_c_mismatch_while_in_flight_is_key_reused_not_in_progress(client: httpx.AsyncClient, fresh_store: InMemoryStore) -> None:
    """[D20] ordering: a mismatched body against an IN-FLIGHT key -> IDEMPOTENCY_KEY_REUSED, not REQUEST_IN_PROGRESS."""
    cart_id = await _cart_with(client, CABLE, 1)
    first = asyncio.create_task(_checkout(client, cart_id, "k"))
    await asyncio.sleep(LATENCY / 4)
    mismatch = await _checkout(client, cart_id, "k", {"customer_id": "cus_3"})
    same = await _checkout(client, cart_id, "k")  # may still be in flight or just completed
    assert mismatch.status_code == 409 and mismatch.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert (await first).status_code == 201
    assert same.status_code in (409, 200)
    if same.status_code == 409:
        assert same.json()["error"]["code"] == "REQUEST_IN_PROGRESS"
    assert len(fresh_store.orders) == 1


async def test_c_key_reuse_does_not_leak_the_order(client: httpx.AsyncClient) -> None:
    cart_id = await _cart_with(client, CABLE, 1)
    order_id = (await _checkout(client, cart_id, "k")).json()["id"]
    r = await _checkout(client, cart_id, "k", {"customer_id": "cus_2"})
    assert order_id not in r.text


# --------------------------------------------------------------------------- (d) retry after failure


async def test_d_retry_after_payment_failure_is_a_new_attempt(fresh_store: InMemoryStore, client_factory) -> None:
    """I3 / C4 (d) [D22]: a failed checkout releases the key; the same key then produces a real order
    (201), not a cached 402 and not IDEMPOTENCY_KEY_REUSED. One app, one registry."""
    gateway = DeclineThenApprove(declines=1)
    async with client_factory(store=fresh_store, payments=gateway) as client:
        cart_id = await _cart_with(client, HEADSET, 1)
        failed = await _checkout(client, cart_id, "k")
        assert failed.status_code == 402 and failed.json()["error"]["code"] == "PAYMENT_DECLINED"
        assert fresh_store.orders == [] and fresh_store.reserved[HEADSET] == 0 and fresh_store.available[HEADSET] == 1
        retry = await _checkout(client, cart_id, "k")
        assert retry.status_code == 201, retry.text
        assert "Idempotent-Replay" not in retry.headers
        assert gateway.calls == 2
        assert len(fresh_store.orders) == 1 and fresh_store.sold[HEADSET] == 1
        replay = await _checkout(client, cart_id, "k")
        assert replay.status_code == 200 and replay.content == retry.content
    _i1(fresh_store)


async def test_d_retry_after_price_changed_then_reprice(client: httpx.AsyncClient, fresh_store: InMemoryStore) -> None:
    """[D22] the motivating case: PRICE_CHANGED -> reprice -> same key -> 201 at the new price."""
    cart_id = await _cart_with(client, CABLE, 2)
    fresh_store.products[CABLE] = dataclasses.replace(fresh_store.products[CABLE], unit_price_minor=45_000)
    failed = await _checkout(client, cart_id, "k")
    assert failed.status_code == 409 and failed.json()["error"]["code"] == "PRICE_CHANGED"
    assert (await client.post(f"/carts/{cart_id}/reprice")).status_code == 200
    ok = await _checkout(client, cart_id, "k")
    assert ok.status_code == 201, ok.text
    assert ok.json()["lines"][0]["unit_price_minor"] == 45_000 and ok.json()["gross_minor"] == 90_000
    assert len(fresh_store.orders) == 1


async def test_d_retry_after_insufficient_inventory_same_key_is_a_new_attempt(client: httpx.AsyncClient, fresh_store: InMemoryStore) -> None:
    """[D22]: a sold-out failure does not poison the key: the retry fails again for the SAME reason,
    freshly evaluated, not as a cached response."""
    winner = await _cart_with(client, HEADSET, 1)
    loser = await _cart_with(client, HEADSET, 1)
    assert (await _checkout(client, winner, "w")).status_code == 201
    for _ in range(2):
        r = await _checkout(client, loser, "l")
        assert r.status_code == 409 and r.json()["error"]["code"] == "INSUFFICIENT_INVENTORY"
        assert "Idempotent-Replay" not in r.headers
    assert len(fresh_store.orders) == 1


# --------------------------------------------------------------------------- scope


async def test_scope_same_key_on_two_carts_is_two_operations(client: httpx.AsyncClient, fresh_store: InMemoryStore) -> None:
    """[D19]: a lazy client reusing one key on every cart still gets one order per cart."""
    a = await _cart_with(client, CABLE, 1)
    b = await _cart_with(client, CABLE, 1)
    ra, rb = await asyncio.gather(_checkout(client, a, "1"), _checkout(client, b, "1"))
    assert ra.status_code == 201 and rb.status_code == 201
    assert ra.json()["id"] != rb.json()["id"] and ra.json()["cart_id"] == a and rb.json()["cart_id"] == b
    assert len(fresh_store.orders) == 2


async def test_scope_new_key_on_checked_out_cart_is_409_not_replay(client: httpx.AsyncClient, fresh_store: InMemoryStore) -> None:
    """I2 vs I3: a second checkout of the same cart with a DIFFERENT key is CART_ALREADY_CHECKED_OUT."""
    cart_id = await _cart_with(client, CABLE, 1)
    assert (await _checkout(client, cart_id, "k1")).status_code == 201
    r = await _checkout(client, cart_id, "k2")
    assert r.status_code == 409 and r.json()["error"]["code"] == "CART_ALREADY_CHECKED_OUT"
    assert len(fresh_store.orders) == 1 and fresh_store.sold[CABLE] == 1
