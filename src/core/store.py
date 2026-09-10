"""InMemoryStore: dicts + the order log + the coupon ledger. [TAD §3.6] [TAD §2] [D33]

Inventory is three counters per product, never a field on Product, so I1
(`stock_total == available + reserved + sold`) is checkable directly. Every mutator is a plain
`def`: the caller holds the relevant lock, and a synchronous block cannot interleave. Every
mutator refuses rather than clamps, and a refused call moves nothing.
"""

from __future__ import annotations

from src.config import SEED_CUSTOMERS, SEED_PRODUCTS, Config
from src.core.errors import InsufficientInventory, ProductNotFound
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
        self.products = {product.id: product for product in SEED_PRODUCTS}
        self.customers = {customer.id: customer for customer in SEED_CUSTOMERS}
        self.carts = {}
        self.orders = []
        self.orders_by_id = {}
        self.coupons = {}
        self.last_rewarded_milestone = 0
        self.available = {product.id: product.stock_total for product in SEED_PRODUCTS}
        self.reserved = {product.id: 0 for product in SEED_PRODUCTS}
        self.sold = {product.id: 0 for product in SEED_PRODUCTS}

    # --- synchronous, caller holds the relevant lock ---
    def reserve_inventory_locked(self, product_id: str, quantity: int) -> None:
        """Raises InsufficientInventory. available -= q; reserved += q."""
        self._check_request(product_id, quantity)
        available = self.available[product_id]
        if quantity > available:
            raise InsufficientInventory(
                "Not enough stock to reserve.",
                {"product_id": product_id, "requested": quantity, "available": available},
            )
        self.available[product_id] = available - quantity
        self.reserved[product_id] += quantity

    def commit_inventory_locked(self, product_id: str, quantity: int) -> None:
        """reserved -= q; sold += q."""
        self._check_request(product_id, quantity)
        self._check_reserved(product_id, quantity)
        self.reserved[product_id] -= quantity
        self.sold[product_id] += quantity

    def release_inventory_locked(self, product_id: str, quantity: int) -> None:
        """reserved -= q; available += q.  [I13]"""
        self._check_request(product_id, quantity)
        self._check_reserved(product_id, quantity)
        self.reserved[product_id] -= quantity
        self.available[product_id] += quantity

    def append_order_locked(self, order: Order) -> None:
        """Append-only, dense: sequence must be len(orders) + 1, ids unique, state PLACED. [I8]"""
        expected_sequence = len(self.orders) + 1
        if order.sequence != expected_sequence:
            raise ValueError(f"order sequence {order.sequence} != expected {expected_sequence}")
        if order.id in self.orders_by_id:
            raise ValueError(f"duplicate order id {order.id}")
        if order.state != "PLACED":
            raise ValueError(f"only PLACED orders enter the log, got {order.state}")
        self.orders.append(order)
        self.orders_by_id[order.id] = order

    # --- lock-free reads; safe because every mutation is synchronous ---
    def placed_order_count(self) -> int:
        """Only PLACED orders count. [I8]"""
        return sum(1 for order in self.orders if order.state == "PLACED")

    def available_quantity(self, product_id: str) -> int:
        self._check_product(product_id)
        return self.available[product_id]

    # --- guards: a refused call moves nothing ---
    def _check_product(self, product_id: str) -> None:
        if product_id not in self.products:
            raise ProductNotFound("No such product.", {"product_id": product_id})

    def _check_request(self, product_id: str, quantity: int) -> None:
        self._check_product(product_id)
        if quantity <= 0:
            raise ValueError(f"quantity must be positive, got {quantity}")

    def _check_reserved(self, product_id: str, quantity: int) -> None:
        reserved = self.reserved[product_id]
        if quantity > reserved:
            raise ValueError(f"{product_id}: cannot move {quantity} out of reserved, only {reserved} reserved")
