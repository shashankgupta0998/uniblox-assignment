"""LockManager: ordered acquisition, the deadlock-freedom guarantee. [TAD §3.3] [TAD §4] [D24]

Three lock classes, one total order:

    products (ascending product_id)  ->  coupon ledger  ->  cart

`acquire` is the only way to obtain a lock. It sorts product ids itself, takes the locks in that
order, and releases in exact reverse. A total order over lock classes makes a wait cycle impossible.
asyncio.Lock is not reentrant, so a caller must never hold a lock it asks for again; duplicate
product ids in one call are collapsed for the same reason.
"""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import AsyncIterator, Iterable, Sequence


class LockManager:
    def __init__(self, product_ids: Iterable[str]) -> None:
        self._product_locks: dict[str, asyncio.Lock] = {pid: asyncio.Lock() for pid in product_ids}
        self._coupon_ledger_lock: asyncio.Lock = asyncio.Lock()
        self._cart_locks: dict[str, asyncio.Lock] = {}

    def acquire(
        self,
        *,
        product_ids: Sequence[str] = (),
        coupon_ledger: bool = False,
        cart_id: str | None = None,
    ) -> AbstractAsyncContextManager[None]:
        """Acquire in the ONE permitted order: products (sorted ascending) -> coupon
        ledger -> cart. Releases in reverse. Ordering is enforced here so callers
        cannot get it wrong. Never acquire a raw lock outside this method.  [D24]"""
        return _hold(self._ordered_locks(product_ids, coupon_ledger, cart_id))

    def _ordered_locks(
        self, product_ids: Sequence[str], coupon_ledger: bool, cart_id: str | None
    ) -> list[asyncio.Lock]:
        """The lock list in acquisition order. Unknown product ids are a caller bug: KeyError."""
        ordered: list[asyncio.Lock] = [self._product_locks[pid] for pid in sorted(set(product_ids))]
        if coupon_ledger:
            ordered.append(self._coupon_ledger_lock)
        if cart_id is not None:
            ordered.append(self._cart_lock(cart_id))
        return ordered

    def _cart_lock(self, cart_id: str) -> asyncio.Lock:
        """Cart locks are created lazily, one per cart_id, and never discarded."""
        lock = self._cart_locks.get(cart_id)
        if lock is None:
            lock = asyncio.Lock()
            self._cart_locks[cart_id] = lock
        return lock


@asynccontextmanager
async def _hold(ordered: list[asyncio.Lock]) -> AsyncIterator[None]:
    """Take every lock in list order; on exit release exactly the ones taken, in reverse."""
    held: list[asyncio.Lock] = []
    try:
        for lock in ordered:
            await lock.acquire()
            held.append(lock)
        yield
    finally:
        for lock in reversed(held):
            lock.release()
