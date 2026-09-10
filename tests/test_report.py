"""C8 — Report reconciliation.  Protects: I8 I11 I12 I15 · [D15] [D31].

After a mixed run (plain orders, a discounted order, failed checkouts of several kinds) the report
reconciles against GET /orders/{id} for every order: item quantities, gross, discount, net.
gross - discount == net.  generated == available + reserved + redeemed.  Two consecutive calls
return byte-identical bodies.  Failed checkouts contribute nothing.  Plus the A8 DoD directly:
build() takes no locks, mutates nothing, counts only PLACED, and the identities hold mid-flight.
"""
from __future__ import annotations

import asyncio
import dataclasses
from collections import Counter
from typing import Any

import httpx
import pytest

from src.core.locks import LockManager
from src.core.models import CouponState, Order, OrderLine
from src.core.payments import AlwaysDeclineGateway, FakePaymentGateway
from src.core.reports import Report, ReportService
from src.core.store import InMemoryStore

CABLE, MOUSE, DOCK, HEADSET, KEYBOARD = "prd_cable", "prd_mouse", "prd_dock", "prd_headset", "prd_keyboard"
OWNER = "cus_2"


async def _cart_with(client: httpx.AsyncClient, *lines: tuple[str, int]) -> str:
    cart_id = (await client.post("/carts")).json()["id"]
    for pid, qty in lines:
        assert (await client.post(f"/carts/{cart_id}/items", json={"product_id": pid, "quantity": qty})).status_code == 201
    return cart_id


async def _checkout(client: httpx.AsyncClient, cart_id: str, key: str, coupon: str | None = None, customer: str = OWNER) -> httpx.Response:
    body: dict[str, Any] = {"customer_id": customer}
    if coupon:
        body["coupon_code"] = coupon
    return await client.post(f"/carts/{cart_id}/checkout", json=body, headers={"Idempotency-Key": key})


def _state_fingerprint(store: InMemoryStore) -> str:
    """Everything observable in the store, rendered deterministically."""
    return repr((
        dict(store.available), dict(store.reserved), dict(store.sold),
        [dataclasses.astuple(o) for o in store.orders], sorted(store.orders_by_id),
        {k: dataclasses.astuple(c) for k, c in store.coupons.items()}, store.last_rewarded_milestone,
        {k: (c.state.value, {p: (i.quantity, i.unit_price_minor) for p, i in c.items.items()}) for k, c in store.carts.items()},
    ))


async def _mixed_run(client: httpx.AsyncClient, store: InMemoryStore, admin_headers: dict[str, str], config) -> tuple[list[str], str]:
    """5 plain orders -> coupon -> 1 discounted order, interleaved with failed checkouts of four kinds."""
    placed: list[str] = []
    for lines in ((CABLE, 3), (MOUSE, 1), (KEYBOARD, 2), (CABLE, 1), (DOCK, 1)):
        cid = await _cart_with(client, lines)
        r = await _checkout(client, cid, f"plain-{cid}")
        assert r.status_code == 201, r.text
        placed.append(r.json()["id"])
    assert (await _checkout(client, await _cart_with(client, (CABLE, 1)), "bad-customer", customer="cus_999")).status_code == 422
    assert (await _checkout(client, (await client.post("/carts")).json()["id"], "empty")).status_code == 422
    drift = await _cart_with(client, (MOUSE, 2))
    store.products[MOUSE] = dataclasses.replace(store.products[MOUSE], unit_price_minor=100_000)
    assert (await _checkout(client, drift, "drift")).status_code == 409
    code = (await client.post("/admin/coupons", headers=admin_headers)).json()["coupon"]["code"]
    stranger = await _cart_with(client, (DOCK, 1))
    assert (await _checkout(client, stranger, "stranger", code, customer="cus_1")).status_code == 422  # COUPON_INVALID
    cid = await _cart_with(client, (DOCK, 2), (HEADSET, 1))
    r = await _checkout(client, cid, "discounted", code)
    assert r.status_code == 201 and r.json()["coupon_code"] == code, r.text
    placed.append(r.json()["id"])
    assert (await _checkout(client, await _cart_with(client, (CABLE, 1)), "already", code)).status_code == 422  # ALREADY_REDEEMED
    return placed, code


# --------------------------------------------------------------------------- the DoD reconciliation


async def test_report_reconciles_against_every_order(app_client: httpx.AsyncClient, fresh_store: InMemoryStore, admin_headers, config) -> None:
    """I8 I11 I12 I15 / C8 DoD: totals and item quantities equal the sum over GET /orders/{id};
    gross - discount == net; generated == available + reserved + redeemed; failures contribute nothing."""
    placed, code = await _mixed_run(app_client, fresh_store, admin_headers, config)
    resp = await app_client.get("/admin/report", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert set(report) == {"orders_placed", "items_purchased", "gross_minor", "discount_minor", "net_minor",
                           "coupons_generated", "coupons_available", "coupons_reserved", "coupons_redeemed", "n", "x"}

    orders = []
    for oid in placed:
        r = await app_client.get(f"/orders/{oid}")
        assert r.status_code == 200
        orders.append(r.json())
    assert report["orders_placed"] == len(orders) == 6 == len(fresh_store.orders)
    assert report["gross_minor"] == sum(o["gross_minor"] for o in orders)
    assert report["discount_minor"] == sum(o["discount_minor"] for o in orders)
    assert report["net_minor"] == sum(o["net_minor"] for o in orders)
    assert report["gross_minor"] - report["discount_minor"] == report["net_minor"]
    for o in orders:
        assert o["gross_minor"] - o["discount_minor"] == o["net_minor"]
    assert report["discount_minor"] == next(o["discount_minor"] for o in orders if o["coupon_code"] == code) > 0
    for v in (report["gross_minor"], report["discount_minor"], report["net_minor"]):
        assert type(v) is int

    quantities: Counter[str] = Counter()
    for o in orders:
        for line in o["lines"]:
            quantities[line["product_id"]] += line["quantity"]
    items = {i["product_id"]: i for i in report["items_purchased"]}
    assert {pid: i["quantity"] for pid, i in items.items() if i["quantity"]} == dict(quantities)
    for pid, item in items.items():
        assert item["name"] == fresh_store.products[pid].name
        assert item["quantity"] == fresh_store.sold[pid]  # I1 corollary: purchased == sold
    assert quantities == {CABLE: 4, MOUSE: 1, KEYBOARD: 2, DOCK: 3, HEADSET: 1}

    assert report["coupons_generated"] == 1 == report["coupons_available"] + report["coupons_reserved"] + report["coupons_redeemed"]
    assert (report["coupons_available"], report["coupons_reserved"], report["coupons_redeemed"]) == (0, 0, 1)
    assert (report["n"], report["x"]) == (config.n, config.x)
    assert all(v == 0 for v in fresh_store.reserved.values())


async def test_two_consecutive_calls_are_byte_identical_and_mutate_nothing(app_client: httpx.AsyncClient, fresh_store: InMemoryStore, admin_headers, config) -> None:
    """I11 / C8 DoD [D31]: the report is a pure read."""
    await _mixed_run(app_client, fresh_store, admin_headers, config)
    before = _state_fingerprint(fresh_store)
    r1 = await app_client.get("/admin/report", headers=admin_headers)
    r2 = await app_client.get("/admin/report", headers=admin_headers)
    assert r1.status_code == r2.status_code == 200
    assert r1.content == r2.content
    assert _state_fingerprint(fresh_store) == before


async def test_failed_checkouts_contribute_nothing(app_client: httpx.AsyncClient, fresh_store: InMemoryStore, admin_headers, config) -> None:
    """I8 / C8 DoD: a report taken before and after a burst of failures is identical."""
    await _mixed_run(app_client, fresh_store, admin_headers, config)
    baseline = (await app_client.get("/admin/report", headers=admin_headers)).content
    for i in range(3):
        assert (await _checkout(app_client, await _cart_with(app_client, (CABLE, 1)), f"f{i}", customer="cus_999")).status_code == 422
    assert (await _checkout(app_client, (await app_client.post("/carts")).json()["id"], "empty-2")).status_code == 422
    drift = await _cart_with(app_client, (KEYBOARD, 1))
    fresh_store.products[KEYBOARD] = dataclasses.replace(fresh_store.products[KEYBOARD], unit_price_minor=1)
    assert (await _checkout(app_client, drift, "drift-2")).status_code == 409
    assert (await app_client.get("/admin/report", headers=admin_headers)).content == baseline
    assert all(v == 0 for v in fresh_store.reserved.values())


async def test_declined_payments_contribute_nothing(fresh_store: InMemoryStore, client_factory, admin_headers) -> None:
    """I8 / I11: declined checkouts (a different app on the same store) leave the report untouched."""
    async with client_factory(store=fresh_store, payments=FakePaymentGateway(latency_seconds=0.0)) as ok_client:
        for _ in range(2):
            assert (await _checkout(ok_client, await _cart_with(ok_client, (CABLE, 2)), "k")).status_code == 201
        baseline = (await ok_client.get("/admin/report", headers=admin_headers)).content
    async with client_factory(store=fresh_store, payments=AlwaysDeclineGateway()) as bad_client:
        for i in range(3):
            assert (await _checkout(bad_client, await _cart_with(bad_client, (DOCK, 1)), f"d{i}")).status_code == 402
        assert (await bad_client.get("/admin/report", headers=admin_headers)).content == baseline
    assert fresh_store.reserved[DOCK] == 0 and fresh_store.sold[DOCK] == 0


async def test_empty_store_report(app_client: httpx.AsyncClient, admin_headers, config) -> None:
    report = (await app_client.get("/admin/report", headers=admin_headers)).json()
    assert report["orders_placed"] == 0 and report["items_purchased"] == []
    assert (report["gross_minor"], report["discount_minor"], report["net_minor"]) == (0, 0, 0)
    assert (report["coupons_generated"], report["coupons_available"], report["coupons_reserved"], report["coupons_redeemed"]) == (0, 0, 0, 0)
    assert (report["n"], report["x"]) == (config.n, config.x)


# --------------------------------------------------------------------------- A8 DoD, directly


def _lock_manager_of(app) -> LockManager:
    return next(v for v in vars(app.state.cart_service).values() if isinstance(v, LockManager))


async def test_build_takes_no_locks(app_client: httpx.AsyncClient, fresh_store: InMemoryStore, config, admin_headers) -> None:
    """A8 DoD [D31]: with every product lock, the ledger lock and a cart lock held, build() returns."""
    await _mixed_run(app_client, fresh_store, admin_headers, config)
    app = app_client._transport.app
    locks = _lock_manager_of(app)
    entered, go = asyncio.Event(), asyncio.Event()

    async def hold_everything() -> None:
        async with locks.acquire(product_ids=list(fresh_store.products), coupon_ledger=True, cart_id="any-cart"):
            entered.set()
            await go.wait()

    holder = asyncio.create_task(hold_everything())
    await asyncio.wait_for(entered.wait(), 0.5)
    report = await asyncio.wait_for(app.state.report_service.build(), 0.5)
    assert isinstance(report, Report) and report.orders_placed == 6
    http = await asyncio.wait_for(app_client.get("/admin/report", headers=admin_headers), 0.5)
    assert http.status_code == 200
    go.set()
    await holder


async def test_build_twice_equal_and_store_untouched(app_client: httpx.AsyncClient, fresh_store: InMemoryStore, config, admin_headers) -> None:
    await _mixed_run(app_client, fresh_store, admin_headers, config)
    svc = ReportService(fresh_store, config)
    before = _state_fingerprint(fresh_store)
    a = await svc.build()
    b = await svc.build()
    assert a == b and dataclasses.is_dataclass(a)
    assert _state_fingerprint(fresh_store) == before
    assert a.gross_minor - a.discount_minor == a.net_minor
    assert a.coupons_generated == a.coupons_available + a.coupons_reserved + a.coupons_redeemed


async def test_counts_only_placed_orders(app_client: httpx.AsyncClient, fresh_store: InMemoryStore, config, admin_headers) -> None:
    """I8 [D15]: a non-PLACED record smuggled into the log is ignored by every total."""
    await _mixed_run(app_client, fresh_store, admin_headers, config)
    before = await ReportService(fresh_store, config).build()
    ghost = Order("ord_ghost", len(fresh_store.orders) + 1, "crt_ghost", OWNER,
                  (OrderLine(KEYBOARD, "Mechanical Keyboard", 9, 449_900, 4_049_100),),
                  4_049_100, 0, 4_049_100, None, None, "CANCELLED")  # type: ignore[arg-type]
    fresh_store.orders.append(ghost)  # bypasses append_order_locked's guard on purpose
    try:
        after = await ReportService(fresh_store, config).build()
    finally:
        fresh_store.orders.remove(ghost)
    assert after == before
    assert after.orders_placed == 6
    assert next(i.quantity for i in after.items_purchased if i.product_id == KEYBOARD) == 2


async def test_identities_hold_mid_flight(fresh_store: InMemoryStore, client_factory, admin_headers, config) -> None:
    """I15 / I12 / I11 during the payment await: the in-flight reservation is visible as
    coupons_reserved == 1 and the identities still hold; the in-flight order is not yet counted."""
    async with client_factory(store=fresh_store, payments=FakePaymentGateway(latency_seconds=0.2)) as client:
        for _ in range(config.n):
            assert (await _checkout(client, await _cart_with(client, (CABLE, 1)), "e")).status_code == 201
        code = (await client.post("/admin/coupons", headers=admin_headers)).json()["coupon"]["code"]
        cart_id = await _cart_with(client, (DOCK, 1))
        inflight = asyncio.create_task(_checkout(client, cart_id, "inflight", code))
        await asyncio.sleep(0.05)  # inside the payment await
        mid = await asyncio.wait_for(ReportService(fresh_store, config).build(), 0.1)  # no lock: returns while checkout holds them
        assert (mid.coupons_generated, mid.coupons_available, mid.coupons_reserved, mid.coupons_redeemed) == (1, 0, 1, 0)
        assert mid.coupons_generated == mid.coupons_available + mid.coupons_reserved + mid.coupons_redeemed
        assert mid.gross_minor - mid.discount_minor == mid.net_minor
        assert mid.orders_placed == config.n and mid.discount_minor == 0
        assert fresh_store.reserved[DOCK] == 1
        products_mid = {p["id"]: p for p in (await asyncio.wait_for(client.get("/products"), 0.1)).json()}
        assert products_mid[DOCK]["stock_total"] == products_mid[DOCK]["available"] + products_mid[DOCK]["reserved"] + products_mid[DOCK]["sold"]
        assert (await inflight).status_code == 201
        after = await ReportService(fresh_store, config).build()
        assert (after.coupons_available, after.coupons_reserved, after.coupons_redeemed) == (0, 0, 1)
        assert after.orders_placed == config.n + 1 and after.discount_minor == 249_990
        assert after.gross_minor - after.discount_minor == after.net_minor
        assert fresh_store.coupons[code].state is CouponState.REDEEMED
