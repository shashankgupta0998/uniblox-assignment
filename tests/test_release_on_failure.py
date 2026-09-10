"""C6 — Release on failure.  Protects: I5 I13 · [D22] [D30].

With AlwaysDeclineGateway: 402 PAYMENT_DECLINED; coupon back to AVAILABLE; reserved == 0 for
every product; available restored to its pre-checkout value; NO order; the idempotency record is
released so the same key can retry.  The coupon survives REPEATED failed checkouts.
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from src.core.errors import PaymentDeclined
from src.core.models import CouponState, Order, OrderLine
from src.core.payments import AlwaysDeclineGateway, PaymentResult
from src.core.store import InMemoryStore

CABLE, DOCK, HEADSET = "prd_cable", "prd_dock", "prd_headset"
OWNER = "cus_2"
LATENCY = 0.03


class CountingDecline(AlwaysDeclineGateway):
    """AlwaysDeclineGateway that counts how often the checkout actually reached the gateway."""

    def __init__(self) -> None:
        self.calls = 0

    async def charge(self, *, amount_minor: int, order_ref: str) -> PaymentResult:
        self.calls += 1
        await asyncio.sleep(LATENCY)
        return await super().charge(amount_minor=amount_minor, order_ref=order_ref)


class DeclineNThenApprove:
    def __init__(self, declines: int) -> None:
        self.declines, self.calls = declines, 0

    async def charge(self, *, amount_minor: int, order_ref: str) -> PaymentResult:
        self.calls += 1
        await asyncio.sleep(LATENCY)
        if self.calls <= self.declines:
            raise PaymentDeclined("declined", {"order_ref": order_ref})
        return PaymentResult(reference=f"pay_{self.calls}")


def _seed_orders(store: InMemoryStore, count: int, customer: str = OWNER) -> None:
    """Append PLACED orders directly so a coupon can be earned without a working gateway."""
    for i in range(count):
        seq = len(store.orders) + 1
        line = OrderLine(CABLE, "USB-C Cable", 1, 39_900, 39_900)
        store.append_order_locked(Order(f"ord_seed_{seq}", seq, f"crt_seed_{seq}", customer, (line,), 39_900, 0, 39_900, None, None, "PLACED"))


def _counters(store: InMemoryStore) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    return dict(store.available), dict(store.reserved), dict(store.sold)


def _i1(store: InMemoryStore) -> None:
    for pid, p in store.products.items():
        assert p.stock_total == store.available[pid] + store.reserved[pid] + store.sold[pid], pid


def _i15(store: InMemoryStore) -> None:
    states = [c.state for c in store.coupons.values()]
    assert len(states) == sum(1 for s in states if s in (CouponState.AVAILABLE, CouponState.RESERVED, CouponState.REDEEMED))


async def _cart_with(client: httpx.AsyncClient, *lines: tuple[str, int]) -> str:
    cart_id = (await client.post("/carts")).json()["id"]
    for pid, qty in lines:
        r = await client.post(f"/carts/{cart_id}/items", json={"product_id": pid, "quantity": qty})
        assert r.status_code == 201, r.text
    return cart_id


async def _checkout(client: httpx.AsyncClient, cart_id: str, key: str, coupon: str | None = None, customer: str = OWNER) -> httpx.Response:
    body: dict[str, Any] = {"customer_id": customer}
    if coupon:
        body["coupon_code"] = coupon
    return await client.post(f"/carts/{cart_id}/checkout", json=body, headers={"Idempotency-Key": key})


async def _earn_coupon(client: httpx.AsyncClient, store: InMemoryStore, admin_headers: dict[str, str], config) -> str:
    _seed_orders(store, config.n)
    r = await client.post("/admin/coupons", headers=admin_headers)
    assert r.status_code == 201, r.text
    assert r.json()["coupon"]["owner_customer_id"] == OWNER
    return r.json()["coupon"]["code"]


@pytest.fixture
def gateway() -> CountingDecline:
    return CountingDecline()


@pytest.fixture
async def declining_client(fresh_store: InMemoryStore, gateway: CountingDecline, client_factory):
    async with client_factory(store=fresh_store, payments=gateway) as c:
        yield c


# --------------------------------------------------------------------------- the DoD case


async def test_declined_payment_releases_everything(declining_client: httpx.AsyncClient, fresh_store: InMemoryStore, gateway: CountingDecline, admin_headers, config) -> None:
    """I5 I13 / C6 DoD: 402 PAYMENT_DECLINED; coupon AVAILABLE; reserved == 0 everywhere; available
    restored; no order; cart still OPEN; the same key can retry (it reaches the gateway again)."""
    code = await _earn_coupon(declining_client, fresh_store, admin_headers, config)
    cart_id = await _cart_with(declining_client, (DOCK, 2), (HEADSET, 1), (CABLE, 5))
    before = _counters(fresh_store)
    orders_before = len(fresh_store.orders)

    r = await _checkout(declining_client, cart_id, "k", code)
    assert r.status_code == 402 and r.json()["error"]["code"] == "PAYMENT_DECLINED", r.text
    assert "Idempotent-Replay" not in r.headers
    assert gateway.calls == 1  # the reservation path really ran up to the gateway

    assert fresh_store.coupons[code].state is CouponState.AVAILABLE
    assert fresh_store.coupons[code].redeemed_by_order_id is None
    assert all(v == 0 for v in fresh_store.reserved.values())
    assert _counters(fresh_store) == before
    assert len(fresh_store.orders) == orders_before
    assert (await declining_client.get(f"/carts/{cart_id}")).json()["state"] == "OPEN"
    _i1(fresh_store)
    _i15(fresh_store)

    # the idempotency record was released: the same key is a NEW attempt, not a cached 402
    r2 = await _checkout(declining_client, cart_id, "k", code)
    assert r2.status_code == 402 and r2.json()["error"]["code"] == "PAYMENT_DECLINED"
    assert gateway.calls == 2
    assert _counters(fresh_store) == before and fresh_store.coupons[code].state is CouponState.AVAILABLE


async def test_coupon_survives_repeated_failed_checkouts(declining_client: httpx.AsyncClient, fresh_store: InMemoryStore, gateway: CountingDecline, admin_headers, config) -> None:
    """I5 / C6 DoD: "a coupon must not be lost" — five declined checkouts, same key and new keys,
    across two carts: coupon AVAILABLE after every one, counters identical, zero orders."""
    code = await _earn_coupon(declining_client, fresh_store, admin_headers, config)
    baseline = _counters(fresh_store)
    cart_a = await _cart_with(declining_client, (HEADSET, 1))
    cart_b = await _cart_with(declining_client, (DOCK, 3), (CABLE, 1))
    attempts = [(cart_a, "same"), (cart_a, "same"), (cart_a, "new-1"), (cart_b, "b-1"), (cart_b, "b-2")]
    for i, (cid, key) in enumerate(attempts, start=1):
        r = await _checkout(declining_client, cid, key, code)
        assert r.status_code == 402, (i, r.text)
        assert fresh_store.coupons[code].state is CouponState.AVAILABLE, i
        assert _counters(fresh_store) == baseline, i
        assert len(fresh_store.orders) == config.n, i
        _i1(fresh_store)
        _i15(fresh_store)
    assert gateway.calls == len(attempts)
    for cid in (cart_a, cart_b):
        assert (await declining_client.get(f"/carts/{cid}")).json()["state"] == "OPEN"


async def test_concurrent_declines_release_all(declining_client: httpx.AsyncClient, fresh_store: InMemoryStore, gateway: CountingDecline) -> None:
    """I13 under concurrency: ten declined checkouts of dock (stock 3) racing -> nothing reserved,
    available back to 3, no orders."""
    carts = [await _cart_with(declining_client, (DOCK, 1)) for _ in range(10)]
    responses = await asyncio.gather(*(_checkout(declining_client, cid, f"k-{i}") for i, cid in enumerate(carts)))
    assert all(r.status_code in (402, 409) for r in responses)
    assert sum(1 for r in responses if r.status_code == 402) >= 3
    assert fresh_store.reserved[DOCK] == 0 and fresh_store.available[DOCK] == 3 and fresh_store.sold[DOCK] == 0
    assert fresh_store.orders == []
    _i1(fresh_store)


async def test_then_a_working_gateway_redeems_the_same_coupon_once(fresh_store: InMemoryStore, client_factory, admin_headers, config) -> None:
    """I5 -> I4: after three declines with one key and coupon, the fourth attempt (same key, same
    coupon) places the order and redeems the coupon exactly once."""
    gw = DeclineNThenApprove(declines=3)
    async with client_factory(store=fresh_store, payments=gw) as client:
        code = await _earn_coupon(client, fresh_store, admin_headers, config)
        cart_id = await _cart_with(client, (HEADSET, 1), (CABLE, 2))
        baseline = _counters(fresh_store)
        for _ in range(3):
            assert (await _checkout(client, cart_id, "k", code)).status_code == 402
            assert fresh_store.coupons[code].state is CouponState.AVAILABLE and _counters(fresh_store) == baseline
        ok = await _checkout(client, cart_id, "k", code)
        assert ok.status_code == 201, ok.text
        assert gw.calls == 4
        order = ok.json()
        assert order["coupon_code"] == code and order["discount_minor"] == 97_970  # 10% of 979_700, half-even
        assert order["gross_minor"] - order["discount_minor"] == order["net_minor"]
        coupon = fresh_store.coupons[code]
        assert coupon.state is CouponState.REDEEMED and coupon.redeemed_by_order_id == order["id"]
        assert fresh_store.reserved[HEADSET] == fresh_store.reserved[CABLE] == 0
        assert fresh_store.sold[HEADSET] == 1 and fresh_store.sold[CABLE] == 2
        assert (await _checkout(client, cart_id, "k", code)).status_code == 200  # replay now
        _i1(fresh_store)
        _i15(fresh_store)


# --------------------------------------------------------------------------- failure after a partial reservation


async def test_inventory_failure_on_second_line_releases_the_first(app_client: httpx.AsyncClient, fresh_store: InMemoryStore, admin_headers, config) -> None:
    """I13 / I5: reserve succeeds for cable, fails for the sold-out headset -> cable released in full,
    coupon untouched, no order (a failure that never reaches the gateway)."""
    code = await _earn_coupon(app_client, fresh_store, admin_headers, config)
    winner = await _cart_with(app_client, (HEADSET, 1))
    loser = await _cart_with(app_client, (CABLE, 4), (HEADSET, 1))
    assert (await _checkout(app_client, winner, "w")).status_code == 201
    before = _counters(fresh_store)
    r = await _checkout(app_client, loser, "l", code)
    assert r.status_code == 409 and r.json()["error"]["code"] == "INSUFFICIENT_INVENTORY"
    assert _counters(fresh_store) == before
    assert fresh_store.reserved[CABLE] == 0 and fresh_store.reserved[HEADSET] == 0
    assert fresh_store.coupons[code].state is CouponState.AVAILABLE
    assert len(fresh_store.orders) == config.n + 1
    _i1(fresh_store)
    _i15(fresh_store)


async def test_unknown_customer_after_nothing_is_reserved(app_client: httpx.AsyncClient, fresh_store: InMemoryStore, admin_headers, config) -> None:
    code = await _earn_coupon(app_client, fresh_store, admin_headers, config)
    cart_id = await _cart_with(app_client, (DOCK, 1))
    before = _counters(fresh_store)
    r = await _checkout(app_client, cart_id, "k", code, customer="cus_999")
    assert r.status_code == 422 and r.json()["error"]["code"] == "UNKNOWN_CUSTOMER"
    assert _counters(fresh_store) == before and fresh_store.coupons[code].state is CouponState.AVAILABLE
