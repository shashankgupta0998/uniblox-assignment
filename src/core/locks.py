"""LockManager: ordered acquisition, the deadlock-freedom guarantee. [TAD §3.3] [TAD §4] [D24]"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Iterable, Sequence


class LockManager:
    def __init__(self, product_ids: Iterable[str]) -> None:
        self._product_ids: tuple[str, ...] = tuple(product_ids)

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
        raise NotImplementedError
