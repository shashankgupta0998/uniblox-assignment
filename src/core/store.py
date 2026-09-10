"""InMemoryStore: dicts + the order log + the coupon ledger. [TAD §3.6] [TAD §2]

A1: attribute shape only. Seeding from Config and the four *_locked mutators are ticket A3.
"""

from __future__ import annotations

from src.config import Config
from src.core.models import Cart, Coupon, Customer, Order, Product


class InMemoryStore:
    products: dict[str, Product]
    customers: dict[str, Customer]
    carts: dict[str, Cart]
    orders: list[Order]  # ordered; index i -> sequence i+1
    orders_by_id: dict[str, Order]
    coupons: dict[str, Coupon]
    last_rewarded_milestone: int
    available: dict[str, int]
    reserved: dict[str, int]
    sold: dict[str, int]

    def __init__(self, config: Config) -> None:
        self._config: Config = config
        self.products = {}
        self.customers = {}
        self.carts = {}
        self.orders = []
        self.orders_by_id = {}
        self.coupons = {}
        self.last_rewarded_milestone = 0
        self.available = {}
        self.reserved = {}
        self.sold = {}

    # --- synchronous, caller holds the relevant lock ---
    def reserve_inventory_locked(self, product_id: str, quantity: int) -> None:
        """Raises InsufficientInventory. available -= q; reserved += q."""
        raise NotImplementedError

    def commit_inventory_locked(self, product_id: str, quantity: int) -> None:
        """reserved -= q; sold += q."""
        raise NotImplementedError

    def release_inventory_locked(self, product_id: str, quantity: int) -> None:
        """reserved -= q; available += q.  [I13]"""
        raise NotImplementedError

    def append_order_locked(self, order: Order) -> None:
        raise NotImplementedError

    # --- lock-free reads; safe because every mutation is synchronous ---
    def placed_order_count(self) -> int:
        raise NotImplementedError

    def available_quantity(self, product_id: str) -> int:
        raise NotImplementedError
