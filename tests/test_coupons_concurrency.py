"""C5 — Coupon race.  Protects: I4 I6 I7 · [D12] [D17].

N concurrent checkouts with ONE coupon -> exactly one 201, N-1 losers, final state REDEEMED with
exactly one redeemed_by_order_id.  N concurrent POST /admin/coupons at one milestone -> exactly one
coupon.  Wrong-owner returns a body BYTE-IDENTICAL to unknown-code.

The FTL DoD named the loser code as 409 COUPON_IN_USE; per the ORCH ruling (option a) the loser
assertion, in its own test (test_losers_get_coupon_already_redeemed), is the design's actual
outcome, 422 COUPON_ALREADY_REDEEMED — see that test's docstring for why COUPON_IN_USE is unreachable.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

import httpx
import pytest

from src.core.models import CouponState
from src.core.payments import FakePaymentGateway
from src.core.store import InMemoryStore

CABLE = "prd_cable"
OWNER = "cus_3"
LATENCY = 0.05
N = 8


def _i15(store: InMemoryStore) -> None:
    by = Counter(c.state for c in store.coupons.values())
    assert len(store.coupons) == by[CouponState.AVAILABLE] + by[CouponState.RESERVED] + by[CouponState.REDEEMED]


def _i1(store: InMemoryStore) -> None:
    for pid, p in store.products.items():
        assert p.stock_total == store.available[pid] + store.reserved[pid] + store.sold[pid], pid


async def _cart_with(client: httpx.AsyncClient, product_id: str = CABLE, quantity: int = 1) -> str:
    cart_id = (await client.post("/carts")).json()["id"]
    r = await client.post(f"/carts/{cart_id}/items", json={"product_id": product_id, "quantity": quantity})
    assert r.status_code == 201, r.text
    return cart_id


async def _checkout(client: httpx.AsyncClient, cart_id: str, key: str, customer: str = OWNER, coupon: str | None = None) -> httpx.Response:
    body: dict[str, Any] = {"customer_id": customer}
    if coupon is not None:
        body["coupon_code"] = coupon
    return await client.post(f"/carts/{cart_id}/checkout", json=body, headers={"Idempotency-Key": key})


async def _earn_coupon(client: httpx.AsyncClient, admin_headers: dict[str, str], config) -> dict[str, Any]:
    """Place n real orders, all by OWNER, so milestone 1 is bound to OWNER whatever the order."""
    carts = [await _cart_with(client) for _ in range(config.n)]
    responses = await asyncio.gather(*(_checkout(client, cid, f"earn-{i}") for i, cid in enumerate(carts)))
    assert all(r.status_code == 201 for r in responses), [r.status_code for r in responses]
    r = await client.post("/admin/coupons", headers=admin_headers)
    assert r.status_code == 201, r.text
    coupon = r.json()["coupon"]
    assert coupon["owner_customer_id"] == OWNER and coupon["state"] == "AVAILABLE" and coupon["milestone"] == 1
    return coupon


@pytest.fixture
async def client(fresh_store: InMemoryStore, client_factory):
    async with client_factory(store=fresh_store, payments=FakePaymentGateway(latency_seconds=LATENCY)) as c:
        yield c


async def _race_for_coupon(client: httpx.AsyncClient, code: str) -> list[httpx.Response]:
    carts = [await _cart_with(client) for _ in range(N)]
    return list(await asyncio.gather(*(_checkout(client, cid, f"race-{i}", OWNER, code) for i, cid in enumerate(carts))))


# --------------------------------------------------------------------------- the race


async def test_one_coupon_n_racers_exactly_one_wins(client: httpx.AsyncClient, fresh_store: InMemoryStore, admin_headers, config) -> None:
    """I4 / C5 DoD: exactly one 201 and N-1 losers; REDEEMED with exactly one redeemed_by_order_id;
    only the winner's order carries the discount."""
    coupon = await _earn_coupon(client, admin_headers, config)
    responses = await _race_for_coupon(client, coupon["code"])
    hist = dict(Counter(r.status_code for r in responses))
    assert hist.get(201) == 1 and sum(v for k, v in hist.items() if k != 201) == N - 1, hist
    winner = next(r for r in responses if r.status_code == 201).json()
    stored = fresh_store.coupons[coupon["code"]]
    assert stored.state is CouponState.REDEEMED
    assert stored.redeemed_by_order_id == winner["id"]
    assert [c.redeemed_by_order_id for c in fresh_store.coupons.values() if c.redeemed_by_order_id] == [winner["id"]]
    assert winner["coupon_code"] == coupon["code"] and winner["discount_percent"] == config.x
    assert winner["discount_minor"] == 3_990 and winner["gross_minor"] == 39_900 and winner["net_minor"] == 35_910  # I9 I12
    discounted = [o for o in fresh_store.orders if o.coupon_code is not None]
    assert [o.id for o in discounted] == [winner["id"]]
    assert len(fresh_store.orders) == config.n + 1  # the n earners + the winner; losers placed nothing
    assert fresh_store.reserved[CABLE] == 0
    _i15(fresh_store)
    _i1(fresh_store)
    for r in responses:
        if r.status_code != 201:
            assert r.status_code == 422 and r.json()["error"]["code"] == "COUPON_ALREADY_REDEEMED", r.text
            assert "Idempotent-Replay" not in r.headers


async def test_losers_get_coupon_already_redeemed(client: httpx.AsyncClient, fresh_store: InMemoryStore, admin_headers, config) -> None:
    """I4: the N-1 losers are 422 COUPON_ALREADY_REDEEMED.

    ORCH ruling (option a, recorded as ORCH's reversible assumption; the human is to record it in
    DECISIONS.md §9): the design stays as built.  Checkout holds the coupon-ledger lock across the
    payment await (TAD §4.5), so no competing checkout can ever observe the coupon RESERVED — by the
    time a loser reaches reserve_locked the winner has committed and the coupon is REDEEMED.
    COUPON_IN_USE is therefore defined in the catalogue but unreachable from checkout in-process;
    the FTL C5 text naming 409 COUPON_IN_USE for the losers described a lock that is not held across
    the await, which this design deliberately does hold."""
    coupon = await _earn_coupon(client, admin_headers, config)
    responses = await _race_for_coupon(client, coupon["code"])
    hist = dict(Counter(r.status_code for r in responses))
    assert hist == {201: 1, 422: N - 1}, hist
    for r in responses:
        if r.status_code != 201:
            assert r.json()["error"]["code"] == "COUPON_ALREADY_REDEEMED", r.text


async def test_losers_carts_stay_open_and_can_check_out_without_the_coupon(client: httpx.AsyncClient, fresh_store: InMemoryStore, admin_headers, config) -> None:
    """I5 / I13: a losing checkout consumed nothing — its cart is OPEN and checks out plainly afterwards."""
    coupon = await _earn_coupon(client, admin_headers, config)
    carts = [await _cart_with(client) for _ in range(N)]
    responses = await asyncio.gather(*(_checkout(client, cid, f"k-{i}", OWNER, coupon["code"]) for i, cid in enumerate(carts)))
    losers = [cid for cid, r in zip(carts, responses) if r.status_code != 201]
    assert len(losers) == N - 1
    for cid in losers:
        assert (await client.get(f"/carts/{cid}")).json()["state"] == "OPEN"
        r = await _checkout(client, cid, f"plain-{cid}", OWNER)
        assert r.status_code == 201 and r.json()["coupon_code"] is None and r.json()["discount_minor"] == 0
    assert fresh_store.coupons[coupon["code"]].state is CouponState.REDEEMED
    _i15(fresh_store)


# --------------------------------------------------------------------------- milestone race


async def test_concurrent_generate_at_one_milestone_creates_exactly_one_coupon(client: httpx.AsyncClient, fresh_store: InMemoryStore, admin_headers, config) -> None:
    """I7 / C5 DoD: N concurrent POST /admin/coupons at one milestone -> exactly one coupon."""
    carts = [await _cart_with(client) for _ in range(config.n)]
    assert all(r.status_code == 201 for r in await asyncio.gather(*(_checkout(client, cid, f"e-{i}") for i, cid in enumerate(carts))))
    responses = await asyncio.gather(*(client.post("/admin/coupons", headers=admin_headers) for _ in range(N)))
    hist = dict(Counter(r.status_code for r in responses))
    assert hist == {201: 1, 409: N - 1}, hist
    assert all(r.json()["error"]["code"] == "NO_ELIGIBLE_MILESTONE" for r in responses if r.status_code == 409)
    assert len(fresh_store.coupons) == 1
    assert next(iter(fresh_store.coupons.values())).milestone == 1
    assert fresh_store.last_rewarded_milestone == 1
    listed = await client.get("/admin/coupons", headers=admin_headers)
    assert len(listed.json()) == 1
    _i15(fresh_store)


async def test_milestone_two_after_five_more_orders_once(client: httpx.AsyncClient, fresh_store: InMemoryStore, admin_headers, config) -> None:
    """I7: milestone 2 is rewarded exactly once, ever, even under a second concurrent burst."""
    for _ in range(2):
        carts = [await _cart_with(client) for _ in range(config.n)]
        assert all(r.status_code == 201 for r in await asyncio.gather(*(_checkout(client, cid, f"{cid}") for cid in carts)))
        responses = await asyncio.gather(*(client.post("/admin/coupons", headers=admin_headers) for _ in range(N)))
        assert Counter(r.status_code for r in responses)[201] == 1
    assert sorted(c.milestone for c in fresh_store.coupons.values()) == [1, 2]


# --------------------------------------------------------------------------- indistinguishability


async def test_wrong_owner_body_is_byte_identical_to_unknown_code(client: httpx.AsyncClient, fresh_store: InMemoryStore, admin_headers, config) -> None:
    """I6 / C5 DoD [D17]: never reveal that another customer's coupon exists."""
    coupon = await _earn_coupon(client, admin_headers, config)
    cart_a = await _cart_with(client)
    cart_b = await _cart_with(client)
    wrong_owner = await _checkout(client, cart_a, "wo", "cus_1", coupon["code"])
    unknown = await _checkout(client, cart_b, "uk", "cus_1", "NOT-A-REAL-CODE")
    assert wrong_owner.status_code == unknown.status_code == 422
    assert wrong_owner.content == unknown.content, (wrong_owner.text, unknown.text)
    assert wrong_owner.json()["error"]["code"] == "COUPON_INVALID"
    assert coupon["code"] not in wrong_owner.text
    # nothing consumed by either failure
    assert fresh_store.coupons[coupon["code"]].state is CouponState.AVAILABLE
    assert fresh_store.reserved[CABLE] == 0 and len(fresh_store.orders) == config.n
    for cid in (cart_a, cart_b):
        assert (await client.get(f"/carts/{cid}")).json()["state"] == "OPEN"
    _i15(fresh_store)


async def test_redeemed_coupon_owner_vs_stranger(client: httpx.AsyncClient, fresh_store: InMemoryStore, admin_headers, config) -> None:
    """I4 / I6: after redemption the owner sees COUPON_ALREADY_REDEEMED; a stranger still sees the
    unknown-code body, byte for byte."""
    coupon = await _earn_coupon(client, admin_headers, config)
    assert (await _checkout(client, await _cart_with(client), "win", OWNER, coupon["code"])).status_code == 201
    owner_again = await _checkout(client, await _cart_with(client), "o2", OWNER, coupon["code"])
    assert owner_again.status_code == 422 and owner_again.json()["error"]["code"] == "COUPON_ALREADY_REDEEMED"
    stranger = await _checkout(client, await _cart_with(client), "s", "cus_1", coupon["code"])
    unknown = await _checkout(client, await _cart_with(client), "u", "cus_1", "NOPE")
    assert stranger.content == unknown.content
    assert fresh_store.coupons[coupon["code"]].state is CouponState.REDEEMED
    assert len([o for o in fresh_store.orders if o.coupon_code]) == 1


# --------------------------------------------------------------------------- I9 at the order level


async def test_hundred_percent_coupon_clamps_discount_to_gross(fresh_store: InMemoryStore, client_factory, admin_headers) -> None:
    """I9 / I12 [D4] at the ORDER level: with x=100 the discount equals the gross exactly, net is 0,
    never negative; the order body and the report reconcile."""
    from src.config import Config

    cfg = Config(x=100, payment_latency_seconds=LATENCY)
    async with client_factory(store=fresh_store, payments=FakePaymentGateway(latency_seconds=LATENCY), config=cfg) as client:
        coupon = await _earn_coupon(client, admin_headers, cfg)
        assert coupon["percent"] == 100
        cart_id = await _cart_with(client, "prd_dock", 1)
        assert (await client.post(f"/carts/{cart_id}/items", json={"product_id": CABLE, "quantity": 3})).status_code == 201
        r = await _checkout(client, cart_id, "full", OWNER, coupon["code"])
        assert r.status_code == 201, r.text
        order = r.json()
        gross = 2_499_900 + 3 * 39_900
        assert order["gross_minor"] == gross
        assert order["discount_minor"] == gross  # clamp bound: min(discount, gross) == gross
        assert order["net_minor"] == 0 and order["net_minor"] >= 0
        assert order["gross_minor"] - order["discount_minor"] == order["net_minor"]
        assert order["discount_percent"] == 100 and order["coupon_code"] == coupon["code"]
        stored = fresh_store.orders_by_id[order["id"]]
        assert (stored.gross_minor, stored.discount_minor, stored.net_minor) == (gross, gross, 0)
        assert (await client.get(f"/orders/{order['id']}")).json() == order
        report = (await client.get("/admin/report", headers=admin_headers)).json()
        assert report["x"] == 100
        assert report["discount_minor"] == gross and report["gross_minor"] - report["discount_minor"] == report["net_minor"]
        assert report["net_minor"] == sum(o.net_minor for o in fresh_store.orders)
        assert fresh_store.coupons[coupon["code"]].state is CouponState.REDEEMED
    _i1(fresh_store)
    _i15(fresh_store)
