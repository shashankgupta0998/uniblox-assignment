"""A5 smoke gate — payments and CartService.  Protects: I2 I10 · [D5] [D6] [D7] [D8] [D9] [D25].

Directly against src/core (CartService + a real InMemoryStore + a real LockManager), plus a few
HTTP round trips through app_client.  Covers: round trips with int money; add on an existing line
increments AND re-snapshots to the live price; set is absolute; remove deletes; price drift is
visible per line (current_unit_price_minor / price_changed / has_price_changes); reprice
re-snapshots every line; unknown cart/product; QUANTITY_EXCEEDS_STOCK is advisory and reserves
nothing; CHECKED_OUT carts reject every mutator; mutators lock products=[pid] THEN cart, never
cart-first; FakePaymentGateway.charge yields even at latency 0; AlwaysDeclineGateway raises.
"""
from __future__ import annotations

import asyncio
import dataclasses
import inspect
import time
from typing import Any

import httpx
import pytest

from src.config import Config
from src.core import payments as payments_mod
from src.core.carts import CartLineView, CartService, CartView
from src.core.errors import (
    CartAlreadyCheckedOut,
    CartNotFound,
    PaymentDeclined,
    ProductNotFound,
    QuantityExceedsStock,
)
from src.core.locks import LockManager
from src.core.models import CartState
from src.core.payments import AlwaysDeclineGateway, FakePaymentGateway, PaymentResult
from src.core.store import InMemoryStore

T = 0.3
CABLE, MOUSE, DOCK, HEADSET = "prd_cable", "prd_mouse", "prd_dock", "prd_headset"
PRICE = {"prd_cable": 39_900, "prd_mouse": 129_900, "prd_dock": 2_499_900, "prd_headset": 899_900}


@pytest.fixture
def locks(fresh_store: InMemoryStore) -> LockManager:
    return LockManager(fresh_store.products.keys())


@pytest.fixture
def carts(fresh_store: InMemoryStore, locks: LockManager) -> CartService:
    return CartService(fresh_store, locks)


def _set_price(store: InMemoryStore, pid: str, price: int) -> None:
    """Simulate an admin price change: Product is frozen, so replace it in the store."""
    store.products[pid] = dataclasses.replace(store.products[pid], unit_price_minor=price)


def _line(view: CartView, pid: str) -> CartLineView:
    return next(l for l in view.lines if l.product_id == pid)


def _i1(store: InMemoryStore) -> None:
    for pid, p in store.products.items():
        assert p.stock_total == store.available[pid] + store.reserved[pid] + store.sold[pid]
        assert store.reserved[pid] >= 0 and store.available[pid] >= 0 and store.sold[pid] >= 0


# =========================================================================== CartService round trips


async def test_create_and_get_empty_cart(carts: CartService, fresh_store: InMemoryStore) -> None:
    view = await carts.create_cart()
    assert isinstance(view, CartView)
    assert view.state is CartState.OPEN and view.lines == () and view.gross_minor == 0 and view.has_price_changes is False
    assert view.id in fresh_store.carts and fresh_store.carts[view.id].state is CartState.OPEN
    again = await carts.get_cart(view.id)
    assert again == view
    other = await carts.create_cart()
    assert other.id != view.id  # [D37] opaque unique ids


async def test_add_item_snapshots_price_and_computes_totals(carts: CartService) -> None:
    """[D5] I14: line total = unit * qty, gross = sum, all int."""
    cart = await carts.create_cart()
    view = await carts.add_item(cart.id, CABLE, 3)
    line = _line(view, CABLE)
    assert (line.product_name, line.quantity, line.unit_price_minor, line.line_total_minor) == ("USB-C Cable", 3, 39_900, 119_700)
    assert line.current_unit_price_minor == 39_900 and line.price_changed is False
    assert view.gross_minor == 119_700 and view.has_price_changes is False
    view = await carts.add_item(cart.id, MOUSE, 2)
    assert [l.product_id for l in view.lines] == [CABLE, MOUSE]  # insertion order
    assert view.gross_minor == 119_700 + 259_800
    for l in view.lines:
        assert type(l.unit_price_minor) is int and type(l.line_total_minor) is int
    assert type(view.gross_minor) is int


async def test_add_item_reserves_nothing(carts: CartService, fresh_store: InMemoryStore) -> None:
    """[D7] add-to-cart reserves nothing; I1 counters untouched."""
    cart = await carts.create_cart()
    await carts.add_item(cart.id, DOCK, 3)
    assert (fresh_store.available[DOCK], fresh_store.reserved[DOCK], fresh_store.sold[DOCK]) == (3, 0, 0)
    _i1(fresh_store)


async def test_add_existing_line_increments_and_resnapshots(carts: CartService, fresh_store: InMemoryStore) -> None:
    """A5 DoD: add_item on an existing line increments quantity AND re-snapshots to the live price."""
    cart = await carts.create_cart()
    await carts.add_item(cart.id, CABLE, 2)
    _set_price(fresh_store, CABLE, 45_000)
    view = await carts.add_item(cart.id, CABLE, 3)
    line = _line(view, CABLE)
    assert line.quantity == 5
    assert line.unit_price_minor == 45_000 and line.line_total_minor == 225_000
    assert line.price_changed is False and view.has_price_changes is False
    assert len(view.lines) == 1


async def test_set_item_quantity_is_absolute_and_resnapshots(carts: CartService, fresh_store: InMemoryStore) -> None:
    cart = await carts.create_cart()
    await carts.add_item(cart.id, CABLE, 5)
    _set_price(fresh_store, CABLE, 41_000)
    view = await carts.set_item_quantity(cart.id, CABLE, 2)
    line = _line(view, CABLE)
    assert line.quantity == 2 and line.unit_price_minor == 41_000 and line.line_total_minor == 82_000
    assert view.gross_minor == 82_000


async def test_set_quantity_on_line_not_in_cart_creates_it(carts: CartService, fresh_store: InMemoryStore) -> None:
    """ORCH ruling at A5: PUT on a product not yet in the cart creates the line — snapshotted to the
    live price, gross updated, nothing reserved.  [D5] [D7]"""
    cart = await carts.create_cart()
    _set_price(fresh_store, MOUSE, 130_000)
    view = await carts.set_item_quantity(cart.id, MOUSE, 1)
    line = _line(view, MOUSE)
    assert (line.quantity, line.unit_price_minor, line.line_total_minor) == (1, 130_000, 130_000)
    assert [l.product_id for l in view.lines] == [MOUSE] and view.gross_minor == 130_000
    assert fresh_store.carts[cart.id].items[MOUSE].quantity == 1
    assert fresh_store.reserved[MOUSE] == 0
    _i1(fresh_store)


async def test_set_quantity_unknown_product_refused(carts: CartService) -> None:
    cart = await carts.create_cart()
    with pytest.raises(ProductNotFound):
        await carts.set_item_quantity(cart.id, "prd_nope", 1)
    assert (await carts.get_cart(cart.id)).lines == ()


async def test_remove_item_deletes_the_line(carts: CartService) -> None:
    cart = await carts.create_cart()
    await carts.add_item(cart.id, CABLE, 1)
    await carts.add_item(cart.id, MOUSE, 1)
    view = await carts.remove_item(cart.id, CABLE)
    assert [l.product_id for l in view.lines] == [MOUSE]
    assert view.gross_minor == 129_900
    view = await carts.remove_item(cart.id, MOUSE)
    assert view.lines == () and view.gross_minor == 0


async def test_remove_missing_line_is_idempotent_noop(carts: CartService) -> None:
    """ORCH ruling at A5: DELETE of a line not in the cart is a no-op (idempotent delete). Unknown
    PRODUCT ids are still refused."""
    cart = await carts.create_cart()
    await carts.add_item(cart.id, MOUSE, 1)
    view = await carts.remove_item(cart.id, CABLE)
    assert [l.product_id for l in view.lines] == [MOUSE]
    with pytest.raises(ProductNotFound):
        await carts.remove_item(cart.id, "prd_nope")


# =========================================================================== price drift + reprice


async def test_get_cart_shows_drift_per_line(carts: CartService, fresh_store: InMemoryStore) -> None:
    """[D5] the snapshot stays; the live price and price_changed are visible before checkout."""
    cart = await carts.create_cart()
    await carts.add_item(cart.id, CABLE, 2)
    await carts.add_item(cart.id, MOUSE, 1)
    _set_price(fresh_store, CABLE, 50_000)
    view = await carts.get_cart(cart.id)
    cable, mouse = _line(view, CABLE), _line(view, MOUSE)
    assert cable.unit_price_minor == 39_900 and cable.line_total_minor == 79_800  # snapshot untouched
    assert cable.current_unit_price_minor == 50_000 and cable.price_changed is True
    assert mouse.price_changed is False and mouse.current_unit_price_minor == 129_900
    assert view.has_price_changes is True
    assert view.gross_minor == 79_800 + 129_900  # gross is on the snapshot


async def test_reprice_resnapshots_every_line_and_clears_flags(carts: CartService, fresh_store: InMemoryStore) -> None:
    """A5 DoD / [D6]: reprice re-snapshots EVERY line to the current price."""
    cart = await carts.create_cart()
    await carts.add_item(cart.id, CABLE, 2)
    await carts.add_item(cart.id, MOUSE, 1)
    await carts.add_item(cart.id, DOCK, 1)
    _set_price(fresh_store, CABLE, 50_000)
    _set_price(fresh_store, DOCK, 2_000_000)
    view = await carts.reprice(cart.id)
    assert _line(view, CABLE).unit_price_minor == 50_000 and _line(view, CABLE).line_total_minor == 100_000
    assert _line(view, DOCK).unit_price_minor == 2_000_000
    assert _line(view, MOUSE).unit_price_minor == 129_900
    assert all(l.price_changed is False for l in view.lines) and view.has_price_changes is False
    assert view.gross_minor == 100_000 + 129_900 + 2_000_000
    assert (await carts.get_cart(cart.id)) == view
    # the store's cart items were really re-snapshotted, not just the view
    assert fresh_store.carts[cart.id].items[CABLE].unit_price_minor == 50_000


async def test_reprice_empty_cart_is_fine(carts: CartService) -> None:
    cart = await carts.create_cart()
    view = await carts.reprice(cart.id)
    assert view.lines == () and view.has_price_changes is False


# =========================================================================== errors


async def test_unknown_cart_raises_cart_not_found(carts: CartService) -> None:
    with pytest.raises(CartNotFound):
        await carts.get_cart("crt_nope")
    with pytest.raises(CartNotFound):
        await carts.add_item("crt_nope", CABLE, 1)
    with pytest.raises(CartNotFound):
        await carts.set_item_quantity("crt_nope", CABLE, 1)
    with pytest.raises(CartNotFound):
        await carts.remove_item("crt_nope", CABLE)
    with pytest.raises(CartNotFound):
        await carts.reprice("crt_nope")


async def test_unknown_product_raises_product_not_found(carts: CartService, fresh_store: InMemoryStore) -> None:
    cart = await carts.create_cart()
    with pytest.raises(ProductNotFound):
        await carts.add_item(cart.id, "prd_nope", 1)
    with pytest.raises(ProductNotFound):
        await carts.set_item_quantity(cart.id, "prd_nope", 1)
    assert (await carts.get_cart(cart.id)).lines == ()
    _i1(fresh_store)


async def test_quantity_exceeds_stock_is_advisory_and_reserves_nothing(carts: CartService, fresh_store: InMemoryStore) -> None:
    """[D7] [D9] I1: above current available -> QuantityExceedsStock; the cart and counters are untouched."""
    cart = await carts.create_cart()
    with pytest.raises(QuantityExceedsStock):
        await carts.add_item(cart.id, HEADSET, 2)
    assert (await carts.get_cart(cart.id)).lines == ()
    assert (fresh_store.available[HEADSET], fresh_store.reserved[HEADSET]) == (1, 0)
    _i1(fresh_store)
    await carts.add_item(cart.id, HEADSET, 1)  # exactly available is fine
    with pytest.raises(QuantityExceedsStock):
        await carts.add_item(cart.id, HEADSET, 1)  # 1 + 1 would exceed stock 1
    assert _line(await carts.get_cart(cart.id), HEADSET).quantity == 1
    with pytest.raises(QuantityExceedsStock):
        await carts.set_item_quantity(cart.id, HEADSET, 2)
    assert _line(await carts.get_cart(cart.id), HEADSET).quantity == 1
    _i1(fresh_store)


async def test_checked_out_cart_rejects_every_mutator(carts: CartService, fresh_store: InMemoryStore) -> None:
    """A5 DoD / I2 [D8]: a CHECKED_OUT cart rejects add/set/remove/reprice; reads still work."""
    cart = await carts.create_cart()
    await carts.add_item(cart.id, CABLE, 1)
    fresh_store.carts[cart.id].state = CartState.CHECKED_OUT
    with pytest.raises(CartAlreadyCheckedOut):
        await carts.add_item(cart.id, MOUSE, 1)
    with pytest.raises(CartAlreadyCheckedOut):
        await carts.set_item_quantity(cart.id, CABLE, 2)
    with pytest.raises(CartAlreadyCheckedOut):
        await carts.remove_item(cart.id, CABLE)
    with pytest.raises(CartAlreadyCheckedOut):
        await carts.reprice(cart.id)
    view = await carts.get_cart(cart.id)
    assert view.state is CartState.CHECKED_OUT and _line(view, CABLE).quantity == 1
    assert list(fresh_store.carts[cart.id].items) == [CABLE]


# =========================================================================== lock order of mutators


async def _blocked(coro_fn, timeout: float = T) -> asyncio.Task:
    """Start coro_fn as a task, let it run until it blocks, assert it has not finished."""
    task = asyncio.create_task(coro_fn())
    for _ in range(10):
        await asyncio.sleep(0)
    await asyncio.sleep(0.02)
    assert not task.done(), "expected the mutator to be blocked on a held lock"
    return task


async def _free(locks: LockManager, **kw) -> bool:
    async def go() -> None:
        async with locks.acquire(**kw):
            pass
    task = asyncio.create_task(go())
    try:
        await asyncio.wait_for(asyncio.shield(task), T)
        return True
    except asyncio.TimeoutError:
        task.cancel()
        try:
            await task
        except BaseException:
            pass
        return False


class _Hold:
    def __init__(self, locks: LockManager, **kw) -> None:
        self.locks, self.kw = locks, kw
        self.entered, self.go = asyncio.Event(), asyncio.Event()

    async def _run(self) -> None:
        async with self.locks.acquire(**self.kw):
            self.entered.set()
            await self.go.wait()

    async def __aenter__(self) -> "_Hold":
        self.task = asyncio.create_task(self._run())
        await asyncio.wait_for(self.entered.wait(), T)
        return self

    async def __aexit__(self, *_: Any) -> None:
        self.go.set()
        await asyncio.wait_for(self.task, T)


@pytest.mark.parametrize("op", ["add_item", "set_item_quantity", "remove_item"])
async def test_mutators_lock_product_then_cart_never_cart_first(carts: CartService, locks: LockManager, op: str) -> None:
    """A5 DoD / [D24]: products=[pid] then cart_id.  Hold the CART: the blocked mutator must already
    hold the PRODUCT lock.  Hold the PRODUCT: the blocked mutator must NOT hold the cart lock."""
    cart = await carts.create_cart()
    await carts.add_item(cart.id, CABLE, 1)

    def call():
        if op == "add_item":
            return carts.add_item(cart.id, CABLE, 1)
        if op == "set_item_quantity":
            return carts.set_item_quantity(cart.id, CABLE, 2)
        return carts.remove_item(cart.id, CABLE)

    async with _Hold(locks, cart_id=cart.id):
        task = await _blocked(call)
        assert not await _free(locks, product_ids=[CABLE]), f"{op}: blocked on the cart but not holding the product lock"
        assert await _free(locks, product_ids=[MOUSE])  # only ITS product is locked
    await asyncio.wait_for(task, T)

    async with _Hold(locks, product_ids=[CABLE]):
        task = await _blocked(call)
        assert await _free(locks, cart_id=cart.id), f"{op}: took the cart lock before the product lock (cart-first)"
    await asyncio.wait_for(task, T)


async def test_reprice_locks_every_line_product_then_cart(carts: CartService, locks: LockManager) -> None:
    """[D6] [D24]: reprice locks all of the cart's products (sorted) then the cart."""
    cart = await carts.create_cart()
    await carts.add_item(cart.id, MOUSE, 1)
    await carts.add_item(cart.id, CABLE, 1)
    async with _Hold(locks, cart_id=cart.id):
        task = await _blocked(lambda: carts.reprice(cart.id))
        assert not await _free(locks, product_ids=[CABLE])
        assert not await _free(locks, product_ids=[MOUSE])
        assert await _free(locks, product_ids=[DOCK])
    await asyncio.wait_for(task, T)
    async with _Hold(locks, product_ids=[CABLE]):
        task = await _blocked(lambda: carts.reprice(cart.id))
        assert await _free(locks, cart_id=cart.id), "reprice took the cart lock before its products"
    await asyncio.wait_for(task, T)


async def test_no_await_inside_mutator_lock_scope_source_audit() -> None:
    """Atomicity rule: inside a CartService lock scope there is no await other than lock acquisition."""
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent.parent / "src" / "core" / "carts.py"
    tree = ast.parse(src.read_text())
    offenders: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncWith):
            for inner in ast.walk(node):
                if inner is not node and isinstance(inner, (ast.Await, ast.AsyncWith, ast.AsyncFor)):
                    offenders.append(inner.lineno)
    assert offenders == [], f"await inside a cart lock scope at carts.py lines {offenders}"


# =========================================================================== payments


def test_gateways_are_async_and_keyword_only() -> None:
    for cls in (FakePaymentGateway, AlwaysDeclineGateway):
        assert inspect.iscoroutinefunction(cls.charge)
        params = inspect.signature(cls.charge).parameters
        assert params["amount_minor"].kind is inspect.Parameter.KEYWORD_ONLY
        assert params["order_ref"].kind is inspect.Parameter.KEYWORD_ONLY


async def test_fake_gateway_returns_non_empty_reference() -> None:
    result = await FakePaymentGateway(latency_seconds=0.0).charge(amount_minor=1_000, order_ref="ord_1")
    assert isinstance(result, PaymentResult) and isinstance(result.reference, str) and result.reference


async def test_fake_gateway_yields_even_at_latency_zero() -> None:
    """A5 DoD / [D25]: charge must await asyncio.sleep even at 0 — another task runs before it returns."""
    gw = FakePaymentGateway(latency_seconds=0.0)
    order: list[str] = []

    async def charge() -> None:
        await gw.charge(amount_minor=1, order_ref="ord_1")
        order.append("charge_done")

    async def other() -> None:
        order.append("other_ran")

    t1 = asyncio.create_task(charge())
    t2 = asyncio.create_task(other())
    await asyncio.gather(t1, t2)
    assert order == ["other_ran", "charge_done"], "charge() returned without yielding: no await asyncio.sleep(0)"


async def test_fake_gateway_awaits_asyncio_sleep_with_its_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[float] = []
    real_sleep = asyncio.sleep

    async def spy(delay: float, *a: Any, **k: Any) -> None:
        calls.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(payments_mod.asyncio, "sleep", spy)
    await FakePaymentGateway(latency_seconds=0.0).charge(amount_minor=1, order_ref="o")
    await FakePaymentGateway(latency_seconds=0.25).charge(amount_minor=1, order_ref="o")
    assert calls == [0.0, 0.25]


async def test_fake_gateway_latency_is_real() -> None:
    gw = FakePaymentGateway(latency_seconds=0.05)
    t0 = time.monotonic()
    await gw.charge(amount_minor=1, order_ref="o")
    assert time.monotonic() - t0 >= 0.045


async def test_always_decline_raises_payment_declined() -> None:
    with pytest.raises(PaymentDeclined) as exc:
        await AlwaysDeclineGateway().charge(amount_minor=1_000, order_ref="ord_1")
    assert exc.value.http_status == 402 and exc.value.code.value == "PAYMENT_DECLINED"


# =========================================================================== HTTP round trips


async def test_http_cart_round_trip(app_client: httpx.AsyncClient, fresh_store: InMemoryStore) -> None:
    cart_id = (await app_client.post("/carts")).json()["id"]
    r = await app_client.post(f"/carts/{cart_id}/items", json={"product_id": CABLE, "quantity": 2})
    assert r.status_code == 201 and r.json()["gross_minor"] == 79_800
    r = await app_client.post(f"/carts/{cart_id}/items", json={"product_id": CABLE, "quantity": 1})
    assert r.status_code == 201 and r.json()["lines"][0]["quantity"] == 3
    r = await app_client.put(f"/carts/{cart_id}/items/{CABLE}", json={"quantity": 1})
    assert r.status_code == 200 and r.json()["lines"][0]["quantity"] == 1 and r.json()["gross_minor"] == 39_900
    _set_price(fresh_store, CABLE, 42_000)
    r = await app_client.get(f"/carts/{cart_id}")
    assert r.json()["lines"][0]["price_changed"] is True and r.json()["has_price_changes"] is True
    assert r.json()["lines"][0]["current_unit_price_minor"] == 42_000 and r.json()["lines"][0]["unit_price_minor"] == 39_900
    r = await app_client.post(f"/carts/{cart_id}/reprice")
    assert r.status_code == 200 and r.json()["lines"][0]["unit_price_minor"] == 42_000 and r.json()["has_price_changes"] is False
    r = await app_client.delete(f"/carts/{cart_id}/items/{CABLE}")
    assert r.status_code == 204
    assert (await app_client.get(f"/carts/{cart_id}")).json()["lines"] == []
    _i1(fresh_store)
