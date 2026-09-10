"""CartService and its view objects. [TAD §3.7] [D5] [D6] [D8]"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.locks import LockManager
from src.core.models import CartState
from src.core.store import InMemoryStore


@dataclass(frozen=True)
class CartLineView:
    product_id: str
    product_name: str
    quantity: int
    unit_price_minor: int
    line_total_minor: int
    current_unit_price_minor: int  # live price, so drift is visible before checkout
    price_changed: bool


@dataclass(frozen=True)
class CartView:
    id: str
    state: CartState
    lines: tuple[CartLineView, ...]
    gross_minor: int
    has_price_changes: bool


class CartService:
    def __init__(self, store: InMemoryStore, locks: LockManager) -> None:
        self._store: InMemoryStore = store
        self._locks: LockManager = locks

    async def create_cart(self) -> CartView:
        raise NotImplementedError

    async def get_cart(self, cart_id: str) -> CartView:
        raise NotImplementedError

    async def add_item(self, cart_id: str, product_id: str, quantity: int) -> CartView:
        raise NotImplementedError

    async def set_item_quantity(self, cart_id: str, product_id: str, quantity: int) -> CartView:
        raise NotImplementedError

    async def remove_item(self, cart_id: str, product_id: str) -> CartView:
        raise NotImplementedError

    async def reprice(self, cart_id: str) -> CartView:
        """Re-snapshot every line to the current price.  [D6]"""
        raise NotImplementedError
