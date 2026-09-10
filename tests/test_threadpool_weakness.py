"""C9 — Threadpool weakness demonstration.  Documents the boundary of I1's (and I2's) guarantee.  [D26]

THIS IS A DOCUMENTED LIMITATION OF THE DESIGN, ON PURPOSE.

The service's mutual exclusion is an `asyncio.Lock`.  It is not thread-safe.  The guarantee
"a synchronous block cannot interleave, and the one await is guarded by the lock" holds only on
a single event loop.  FastAPI runs a plain `def` handler in a threadpool, so a single mis-declared
handler would put the checkout on a thread — and that is why every state-touching handler in
src/api is `async def`.  These tests drive CheckoutService from a ThreadPoolExecutor and show the
invariants breaking, deterministically, so the boundary is demonstrated rather than merely stated.

How the threads are modelled (nothing in src/ is edited):
  * Each worker thread runs the coroutine with `asyncio.run`, i.e. on its own event loop, exactly
    as a `def` handler calling into the service would.  A LockManager holds asyncio.Locks, and an
    asyncio.Lock cannot be shared across loops: a waiter parks on a future bound to one loop and is
    never woken from another — sharing ONE LockManager across the threads hangs the process
    (verified while writing this file; a hung thread cannot be left in a test suite).  So each
    thread builds its own LockManager around the ONE shared store, which is precisely the
    protection a thread actually gets: none.
  * A blocking `time.sleep` stands in for blocking I/O — the payment call (test 1) and the
    check-then-decrement of a reservation (test 2).  Blocking sleep releases the GIL, so other
    threads run inside the critical section.  No `sys.setswitchinterval` tuning is needed; a
    pure-CPU race would need it and would read as contrived.

Observed at default settings, three consecutive runs: 100 checkouts on 32 workers ->
  test 1: one cart checked out 32 times (32 orders), where the guarantee says 1;
  test 2: stock 1 sold 32 times, `stock_total == available + reserved + sold` broken.
Each test carries a single-event-loop control that shows the same code and the same sleeps
giving the correct answer where the design applies.
"""
from __future__ import annotations

import asyncio
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait

import pytest

from src.config import Config
from src.core.carts import CartService
from src.core.checkout import CheckoutService
from src.core.coupons import CouponService
from src.core.errors import InsufficientInventory
from src.core.idempotency import IdempotencyRegistry
from src.core.locks import LockManager
from src.core.payments import PaymentResult
from src.core.store import InMemoryStore

N = 100
WORKERS = 32
CABLE, HEADSET = "prd_cable", "prd_headset"


class BlockingGateway:
    """A payment call that BLOCKS the thread (what real I/O in a def handler looks like)."""

    async def charge(self, *, amount_minor: int, order_ref: str) -> PaymentResult:
        time.sleep(0.02)
        return PaymentResult(reference=order_ref)


def _service(store: InMemoryStore, config: Config, locks: LockManager) -> CheckoutService:
    return CheckoutService(store, locks, IdempotencyRegistry(), CouponService(store, locks, config), BlockingGateway(), config)


async def _cart_async(store: InMemoryStore, product_id: str, quantity: int) -> str:
    carts = CartService(store, LockManager(store.products.keys()))
    view = await carts.create_cart()
    await carts.add_item(view.id, product_id, quantity)
    return view.id


def _cart(store: InMemoryStore, config: Config, product_id: str, quantity: int) -> str:
    return asyncio.run(_cart_async(store, product_id, quantity))


def _from_threads(store: InMemoryStore, config: Config, cart_ids: list[str]) -> Counter:
    """Each thread: its own event loop, its own LockManager, the shared store."""

    def worker(i: int) -> str:
        locks = LockManager(store.products.keys())
        svc = _service(store, config, locks)
        try:
            asyncio.run(svc.checkout(cart_id=cart_ids[i], customer_id="cus_1", coupon_code=None, idempotency_key=f"k-{i}"))
            return "OK"
        except Exception as exc:  # noqa: BLE001 — we are counting outcomes
            return type(exc).__name__

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = [ex.submit(worker, i) for i in range(N)]
        done, not_done = wait(futures, timeout=60)
    assert not not_done, "threads hung — the demonstration must never leave a thread behind"
    return Counter(f.result() for f in done)


async def _from_one_loop(store: InMemoryStore, config: Config, cart_ids: list[str]) -> Counter:
    """The control: the same calls, the same blocking sleeps, ONE event loop, ONE LockManager."""
    svc = _service(store, config, LockManager(store.products.keys()))

    async def one(i: int) -> str:
        try:
            await svc.checkout(cart_id=cart_ids[i], customer_id="cus_1", coupon_code=None, idempotency_key=f"k-{i}")
            return "OK"
        except Exception as exc:  # noqa: BLE001
            return type(exc).__name__

    return Counter(await asyncio.gather(*(one(i) for i in range(N))))


def _i1_holds(store: InMemoryStore, pid: str) -> bool:
    return store.products[pid].stock_total == store.available[pid] + store.reserved[pid] + store.sold[pid]


# --------------------------------------------------------------------------- 1. I2 breaks


def test_threads_check_out_one_cart_many_times(config: Config) -> None:
    """[D26] boundary of I2: from a threadpool, one cart is checked out by many threads at once —
    each passes the OPEN check, blocks in the payment, then commits.  The single-loop control below
    gives exactly one order."""
    store = InMemoryStore(config)
    cart_id = _cart(store, config, CABLE, 1)
    outcomes = _from_threads(store, config, [cart_id] * N)
    assert outcomes["OK"] >= 2, outcomes
    assert len(store.orders) == outcomes["OK"] >= 2
    assert store.sold[CABLE] == outcomes["OK"] >= 2, "one cart line of quantity 1 sold many times"
    assert all(o.cart_id == cart_id for o in store.orders)


async def test_control_one_loop_checks_out_one_cart_once(config: Config) -> None:
    """I2 where the design applies: same code, same blocking gateway, one loop -> exactly one order."""
    store = InMemoryStore(config)
    cart_id = await _cart_async(store, CABLE, 1)
    outcomes = await _from_one_loop(store, config, [cart_id] * N)
    assert outcomes == {"OK": 1, "CartAlreadyCheckedOut": N - 1}, outcomes
    assert len(store.orders) == 1 and store.sold[CABLE] == 1


# --------------------------------------------------------------------------- 2. I1 breaks


def _blocking_reserve(store: InMemoryStore):
    """The store's reserve step as a def handler would execute it: read, block on I/O, write.
    Installed on the INSTANCE (never an edit to src/).  On one loop this is still atomic because
    the sleep blocks the loop; on threads the sleep releases the GIL between check and decrement."""

    def reserve(product_id: str, quantity: int) -> None:
        available = store.available[product_id]
        time.sleep(0.005)
        if quantity > available:
            raise InsufficientInventory("Not enough stock to reserve.", {"product_id": product_id})
        store.available[product_id] = available - quantity
        store.reserved[product_id] += quantity

    return reserve


def test_threads_oversell_stock_one(config: Config) -> None:
    """[D26] boundary of I1: stock 1, 100 carts, threads -> sold > 1 and the identity is broken."""
    store = InMemoryStore(config)
    cart_ids = [_cart(store, config, HEADSET, 1) for _ in range(N)]
    store.reserve_inventory_locked = _blocking_reserve(store)  # type: ignore[method-assign]
    outcomes = _from_threads(store, config, cart_ids)
    assert outcomes["OK"] >= 2, outcomes
    assert store.sold[HEADSET] == outcomes["OK"] >= 2, "stock 1 was sold more than once"
    assert store.sold[HEADSET] > store.products[HEADSET].stock_total
    assert not _i1_holds(store, HEADSET), "stock_total == available + reserved + sold survived the threads"
    assert len(store.orders) == outcomes["OK"]


async def test_control_one_loop_never_oversells(config: Config) -> None:
    """I1 where the design applies: same blocking reserve, one loop -> exactly one sale, identity intact."""
    store = InMemoryStore(config)
    cart_ids = [await _cart_async(store, HEADSET, 1) for _ in range(N)]
    store.reserve_inventory_locked = _blocking_reserve(store)  # type: ignore[method-assign]
    outcomes = await _from_one_loop(store, config, cart_ids)
    assert outcomes == {"OK": 1, "InsufficientInventory": N - 1}, outcomes
    assert store.sold[HEADSET] == 1 and _i1_holds(store, HEADSET)


def test_the_documented_reason_every_handler_is_async() -> None:
    """[D26] the production guard against this whole file: no plain-def route handler exists."""
    import inspect
    from fastapi.routing import APIRoute

    from src.main import app

    for route in app.routes:
        if isinstance(route, APIRoute):
            assert inspect.iscoroutinefunction(route.endpoint), f"{route.path} is a def handler: the threadpool weakness is LIVE"
