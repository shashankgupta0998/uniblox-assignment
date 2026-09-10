"""A3 smoke gate — store counters and the LockManager.  Protects: I1 I10 · [D24] [D33].

Directly against src/core, no HTTP.  Two halves:
  1. I1  `stock_total == available + reserved + sold`, all >= 0, after EVERY *_locked mutator,
     including the refusing paths (over-reserve, over-commit, over-release, unknown product).
  2. [D24] LockManager.acquire takes products (sorted ascending) -> coupon ledger -> cart and
     releases in reverse.  Proven by contention, not by peeking at private attributes: hold one
     lock, start a contender that needs a superset, and observe which OTHER locks the contender
     has (or has not) taken while blocked.
"""
from __future__ import annotations

import asyncio
import inspect
import re
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from src.config import SEED_CUSTOMERS, SEED_PRODUCTS, Config
from src.core.errors import DomainError, InsufficientInventory
from src.core.locks import LockManager
from src.core.models import Order, OrderLine
from src.core.store import InMemoryStore

SRC = Path(__file__).resolve().parent.parent.parent / "src"
PIDS = [p.id for p in SEED_PRODUCTS]
STOCK = {p.id: p.stock_total for p in SEED_PRODUCTS}
T = 0.3  # seconds: "did not acquire within T" == blocked
REFUSED = (ValueError, KeyError, AssertionError, DomainError)  # a stub's NotImplementedError never counts


def _i1(store: InMemoryStore) -> None:
    """I1 for every product, plus non-negativity."""
    for pid, product in store.products.items():
        a, r, s = store.available[pid], store.reserved[pid], store.sold[pid]
        assert a >= 0 and r >= 0 and s >= 0, (pid, a, r, s)
        assert product.stock_total == a + r + s, (pid, product.stock_total, a, r, s)
    assert set(store.available) == set(store.reserved) == set(store.sold) == set(store.products)


def _order(seq: int, pid: str = "prd_cable", qty: int = 1) -> Order:
    price = STOCK and next(p.unit_price_minor for p in SEED_PRODUCTS if p.id == pid)
    return Order(
        id=f"ord_{seq}", sequence=seq, cart_id=f"crt_{seq}", customer_id="cus_1",
        lines=(OrderLine(pid, "x", qty, price, price * qty),),
        gross_minor=price * qty, discount_minor=0, net_minor=price * qty,
        coupon_code=None, discount_percent=None, state="PLACED",
    )


# =========================================================================== 1. store / I1


def test_seeding_contract(fresh_store: InMemoryStore) -> None:
    """InMemoryStore(config) seeds products/customers; available == stock_total, reserved == sold == 0."""
    assert [p.id for p in fresh_store.products.values()] == PIDS
    assert fresh_store.products["prd_headset"] is not None
    assert list(fresh_store.customers) == [c.id for c in SEED_CUSTOMERS]
    for pid in PIDS:
        assert fresh_store.available[pid] == STOCK[pid]
        assert fresh_store.reserved[pid] == 0 and fresh_store.sold[pid] == 0
    assert fresh_store.orders == [] and fresh_store.orders_by_id == {} and fresh_store.coupons == {}
    assert fresh_store.last_rewarded_milestone == 0
    assert fresh_store.placed_order_count() == 0
    _i1(fresh_store)


def test_locked_mutators_are_synchronous() -> None:
    """A3 DoD: every *_locked method is def, not async def."""
    for name in ("reserve_inventory_locked", "commit_inventory_locked", "release_inventory_locked", "append_order_locked"):
        fn = getattr(InMemoryStore, name)
        assert not inspect.iscoroutinefunction(fn), name
        assert not inspect.isasyncgenfunction(fn), name
    assert not inspect.iscoroutinefunction(LockManager.acquire)


@pytest.mark.parametrize("pid", PIDS)
def test_i1_after_reserve_commit_release(fresh_store: InMemoryStore, pid: str) -> None:
    """I1 after each of reserve -> commit, and reserve -> release, for every product."""
    s = fresh_store
    total = STOCK[pid]
    q = 1
    s.reserve_inventory_locked(pid, q)
    assert (s.available[pid], s.reserved[pid], s.sold[pid]) == (total - q, q, 0)
    _i1(s)
    s.commit_inventory_locked(pid, q)
    assert (s.available[pid], s.reserved[pid], s.sold[pid]) == (total - q, 0, q)
    _i1(s)
    if total - q >= 1:
        s.reserve_inventory_locked(pid, 1)
        assert (s.available[pid], s.reserved[pid], s.sold[pid]) == (total - q - 1, 1, q)
        _i1(s)
        s.release_inventory_locked(pid, 1)
        assert (s.available[pid], s.reserved[pid], s.sold[pid]) == (total - q, 0, q)
        _i1(s)
    assert s.available_quantity(pid) == s.available[pid]


def test_reserve_exactly_available_then_one_more_refused(fresh_store: InMemoryStore) -> None:
    """I1: reserve up to available succeeds; the next unit raises InsufficientInventory, unchanged."""
    s = fresh_store
    s.reserve_inventory_locked("prd_dock", 3)
    assert (s.available["prd_dock"], s.reserved["prd_dock"]) == (0, 3)
    _i1(s)
    with pytest.raises(InsufficientInventory):
        s.reserve_inventory_locked("prd_dock", 1)
    assert (s.available["prd_dock"], s.reserved["prd_dock"], s.sold["prd_dock"]) == (0, 3, 0)
    _i1(s)


@pytest.mark.parametrize("qty", [2, 3, 100, 10**6])
def test_over_reserve_raises_and_leaves_counters_unchanged(fresh_store: InMemoryStore, qty: int) -> None:
    """I1 / never oversell: quantity > available -> InsufficientInventory; no counter moves."""
    s = fresh_store
    before = (s.available["prd_headset"], s.reserved["prd_headset"], s.sold["prd_headset"])
    with pytest.raises(InsufficientInventory):
        s.reserve_inventory_locked("prd_headset", qty)
    assert (s.available["prd_headset"], s.reserved["prd_headset"], s.sold["prd_headset"]) == before == (1, 0, 0)
    _i1(s)


def test_over_commit_and_over_release_never_go_negative(fresh_store: InMemoryStore) -> None:
    """I1: committing or releasing more than is reserved must be refused, not clamped."""
    s = fresh_store
    s.reserve_inventory_locked("prd_mouse", 2)
    with pytest.raises(REFUSED):
        s.commit_inventory_locked("prd_mouse", 3)
    assert (s.available["prd_mouse"], s.reserved["prd_mouse"], s.sold["prd_mouse"]) == (38, 2, 0)
    _i1(s)
    with pytest.raises(REFUSED):
        s.release_inventory_locked("prd_mouse", 3)
    assert (s.available["prd_mouse"], s.reserved["prd_mouse"], s.sold["prd_mouse"]) == (38, 2, 0)
    _i1(s)
    with pytest.raises(REFUSED):
        s.commit_inventory_locked("prd_keyboard", 1)  # nothing reserved
    _i1(s)
    with pytest.raises(REFUSED):
        s.release_inventory_locked("prd_keyboard", 1)
    _i1(s)


@pytest.mark.parametrize("qty", [0, -1])
def test_non_positive_quantities_refused(fresh_store: InMemoryStore, qty: int) -> None:
    """I1: a zero or negative quantity cannot be used to move counters backwards."""
    s = fresh_store
    for fn in (s.reserve_inventory_locked, s.commit_inventory_locked, s.release_inventory_locked):
        with pytest.raises(REFUSED):
            fn("prd_cable", qty)
        _i1(s)
    assert (s.available["prd_cable"], s.reserved["prd_cable"], s.sold["prd_cable"]) == (100, 0, 0)


def test_unknown_product_refused(fresh_store: InMemoryStore) -> None:
    with pytest.raises(REFUSED):
        fresh_store.reserve_inventory_locked("prd_nope", 1)
    assert "prd_nope" not in fresh_store.available
    _i1(fresh_store)


def test_append_order_sequence_and_index(fresh_store: InMemoryStore) -> None:
    """append_order_locked: orders[i].sequence == i+1, orders_by_id populated, placed_order_count.  I8"""
    s = fresh_store
    s.append_order_locked(_order(1))
    s.append_order_locked(_order(2, "prd_mouse", 2))
    s.append_order_locked(_order(3))
    assert [o.sequence for o in s.orders] == [1, 2, 3]
    for i, o in enumerate(s.orders):
        assert o.sequence == i + 1
        assert s.orders_by_id[o.id] is o
    assert s.placed_order_count() == 3
    assert len(s.orders_by_id) == 3
    _i1(s)  # appending an order moves no inventory counter by itself


def test_append_order_rejects_wrong_sequence_or_duplicate_id(fresh_store: InMemoryStore) -> None:
    """The order log is append-only and dense: sequence must be len+1, ids unique."""
    s = fresh_store
    s.append_order_locked(_order(1))
    with pytest.raises(REFUSED):
        s.append_order_locked(_order(1))  # duplicate id / sequence
    with pytest.raises(REFUSED):
        s.append_order_locked(_order(5))  # gap
    assert [o.sequence for o in s.orders] == [1]
    assert s.placed_order_count() == 1


def test_full_lifecycle_identity(fresh_store: InMemoryStore) -> None:
    """I1 across a realistic sequence: reserve two lines, commit one, release one, append order."""
    s = fresh_store
    s.reserve_inventory_locked("prd_dock", 2)
    s.reserve_inventory_locked("prd_headset", 1)
    _i1(s)
    s.commit_inventory_locked("prd_dock", 2)
    s.release_inventory_locked("prd_headset", 1)
    _i1(s)
    s.append_order_locked(_order(1, "prd_dock", 2))
    _i1(s)
    assert (s.available["prd_dock"], s.reserved["prd_dock"], s.sold["prd_dock"]) == (1, 0, 2)
    assert (s.available["prd_headset"], s.reserved["prd_headset"], s.sold["prd_headset"]) == (1, 0, 0)


def test_orders_are_immutable(fresh_store: InMemoryStore) -> None:
    """I10: Order and OrderLine are frozen; a placed order cannot be edited."""
    o = _order(1)
    with pytest.raises(FrozenInstanceError):
        o.net_minor = 0  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        o.lines[0].unit_price_minor = 1  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        fresh_store.products["prd_cable"].unit_price_minor = 1  # type: ignore[misc]


# =========================================================================== 2. LockManager / D24


async def _try(lm: LockManager, **kw) -> bool:
    """True if acquire(**kw) succeeds within T seconds (then released), False if it blocks."""
    async def go() -> None:
        async with lm.acquire(**kw):
            pass
    task = asyncio.create_task(go())
    try:
        await asyncio.wait_for(asyncio.shield(task), T)
        return True
    except asyncio.TimeoutError:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        return False


async def _enter_exit(lm: LockManager, **kw) -> bool:
    """A contender with NO timeout: acquire(**kw), release immediately, report True."""
    async with lm.acquire(**kw):
        pass
    return True


class _Holder:
    """Hold a lock set open until released, from a background task."""

    def __init__(self, lm: LockManager, **kw) -> None:
        self.lm, self.kw = lm, kw
        self.entered = asyncio.Event()
        self.go = asyncio.Event()
        self.task: asyncio.Task | None = None

    async def _run(self) -> None:
        async with self.lm.acquire(**self.kw):
            self.entered.set()
            await self.go.wait()

    async def __aenter__(self) -> "_Holder":
        self.task = asyncio.create_task(self._run())
        await asyncio.wait_for(self.entered.wait(), T)
        return self

    async def __aexit__(self, *exc) -> None:
        self.go.set()
        assert self.task is not None
        await asyncio.wait_for(self.task, T)


@pytest.fixture
def lm() -> LockManager:
    return LockManager(PIDS)


async def test_acquire_returns_async_context_manager_and_noop_with_no_args(lm: LockManager) -> None:
    cm = lm.acquire()
    assert hasattr(cm, "__aenter__") and hasattr(cm, "__aexit__")
    assert not inspect.iscoroutine(cm)
    async with cm:
        async with lm.acquire():  # nested no-op must not deadlock
            pass
    assert await _try(lm, product_ids=PIDS, coupon_ledger=True, cart_id="c")


async def test_same_lock_blocks_different_locks_do_not(lm: LockManager) -> None:
    async with _Holder(lm, product_ids=["prd_dock"], coupon_ledger=True, cart_id="c1"):
        assert not await _try(lm, product_ids=["prd_dock"])
        assert not await _try(lm, coupon_ledger=True)
        assert not await _try(lm, cart_id="c1")
        assert await _try(lm, product_ids=["prd_headset"])
        assert await _try(lm, cart_id="c2")  # cart locks are per cart_id, created lazily
    assert await _try(lm, product_ids=["prd_dock"], coupon_ledger=True, cart_id="c1")


async def test_products_are_taken_in_sorted_order_regardless_of_input_order(lm: LockManager) -> None:
    """[D24] Hold prd_dock (sorts before prd_headset).  A contender asking for [headset, dock] must
    block on dock FIRST and therefore must NOT be holding headset."""
    async with _Holder(lm, product_ids=["prd_dock"]):
        contender = asyncio.create_task(_enter_exit(lm, product_ids=["prd_headset", "prd_dock"]))
        for _ in range(5):
            await asyncio.sleep(0)
        assert await _try(lm, product_ids=["prd_headset"]), "contender took prd_headset before prd_dock: not sorted"
        assert not contender.done()
    assert await asyncio.wait_for(contender, T)  # once dock is released the contender completes


async def test_products_before_coupon_ledger(lm: LockManager) -> None:
    """[D24] products -> ledger.  Hold the ledger; a contender wanting {cable, ledger} holds cable
    while blocked on the ledger.  Hold cable instead; the contender must not be holding the ledger."""
    async with _Holder(lm, coupon_ledger=True):
        contender = asyncio.create_task(_enter_exit(lm, product_ids=["prd_cable"], coupon_ledger=True))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not await _try(lm, product_ids=["prd_cable"]), "contender did not take the product before the ledger"
        assert not contender.done()
    assert await asyncio.wait_for(contender, T)
    async with _Holder(lm, product_ids=["prd_cable"]):
        contender = asyncio.create_task(_enter_exit(lm, product_ids=["prd_cable"], coupon_ledger=True))
        for _ in range(5):
            await asyncio.sleep(0)
        assert await _try(lm, coupon_ledger=True), "contender took the ledger before the product: wrong order"
        assert not contender.done()
    assert await asyncio.wait_for(contender, T)


async def test_coupon_ledger_before_cart(lm: LockManager) -> None:
    """[D24] ledger -> cart.  Hold cart c1; a contender wanting {ledger, c1} holds the ledger while
    blocked on the cart.  Hold the ledger instead; the contender must not be holding c1."""
    async with _Holder(lm, cart_id="c1"):
        contender = asyncio.create_task(_enter_exit(lm, coupon_ledger=True, cart_id="c1"))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not await _try(lm, coupon_ledger=True), "contender did not take the ledger before the cart"
        assert not contender.done()
    assert await asyncio.wait_for(contender, T)
    async with _Holder(lm, coupon_ledger=True):
        contender = asyncio.create_task(_enter_exit(lm, coupon_ledger=True, cart_id="c1"))
        for _ in range(5):
            await asyncio.sleep(0)
        assert await _try(lm, cart_id="c1"), "contender took the cart before the ledger: wrong order"
        assert not contender.done()
    assert await asyncio.wait_for(contender, T)


async def test_release_in_reverse_and_everything_freed_after_exit(lm: LockManager) -> None:
    """After the context exits every lock is free again, including after an exception inside."""
    with pytest.raises(RuntimeError):
        async with lm.acquire(product_ids=["prd_dock", "prd_cable"], coupon_ledger=True, cart_id="c1"):
            raise RuntimeError("boom")
    for kw in ({"product_ids": ["prd_dock"]}, {"product_ids": ["prd_cable"]}, {"coupon_ledger": True}, {"cart_id": "c1"}):
        assert await _try(lm, **kw), kw


async def test_opposite_order_contenders_do_not_deadlock_and_serialise(lm: LockManager) -> None:
    """[D24] Two coroutines locking {dock, headset} in opposite input orders, 50 rounds each, with an
    await inside: no deadlock, and their critical sections never overlap."""
    events: list[tuple[str, str]] = []
    in_section = 0
    max_in_section = 0

    async def worker(name: str, ids: list[str]) -> None:
        nonlocal in_section, max_in_section
        for _ in range(50):
            async with lm.acquire(product_ids=ids, coupon_ledger=True, cart_id=name):
                in_section += 1
                max_in_section = max(max_in_section, in_section)
                events.append((name, "in"))
                await asyncio.sleep(0)
                events.append((name, "out"))
                in_section -= 1

    await asyncio.wait_for(asyncio.gather(
        worker("a", ["prd_dock", "prd_headset"]),
        worker("b", ["prd_headset", "prd_dock"]),
    ), 5.0)
    assert max_in_section == 1
    assert len(events) == 200


async def test_lock_manager_is_per_instance(lm: LockManager) -> None:
    """[D27]-adjacent: two LockManagers do not share locks (no module-level lock state)."""
    other = LockManager(PIDS)
    async with _Holder(lm, product_ids=["prd_dock"], coupon_ledger=True):
        assert await _try(other, product_ids=["prd_dock"], coupon_ledger=True)


# =========================================================================== 3. no other lock path


def test_no_lock_acquisition_outside_locks_py() -> None:
    """A3 DoD: there is no other way to obtain a lock — asyncio.Lock and .acquire( live in locks.py only."""
    offenders = []
    for py in sorted((SRC / "core").glob("*.py")) + [SRC / "config.py"]:
        if py.name == "locks.py":
            continue
        for lineno, line in enumerate(py.read_text().splitlines(), start=1):
            code = line.split("#", 1)[0]
            if re.search(r"asyncio\.Lock\(|\bLock\(\)|\bSemaphore\(|\bCondition\(|threading\.", code):
                offenders.append((py.name, lineno, code.strip()))
            if re.search(r"\.acquire\(", code) and "locks.acquire(" not in code and "_locks.acquire(" not in code:
                offenders.append((py.name, lineno, code.strip()))
    assert offenders == [], offenders


def test_locks_py_uses_asyncio_lock_and_nothing_threaded() -> None:
    text = (SRC / "core" / "locks.py").read_text()
    assert "asyncio.Lock" in text
    assert "threading" not in text and "RLock" not in text
