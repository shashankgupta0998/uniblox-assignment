"""ReportService: a pure, lock-free read over the store. [TAD §3.10] [D31]

No lock is taken and nothing is mutated. Safe because every mutation in the system is a
synchronous block: a reader on the same event loop sees either none of a commit or all of it.
`coupons_reserved` is exposed precisely so `generated == available + reserved + redeemed`
holds while a checkout is mid-flight. [I11] [I12] [I15]
"""

from __future__ import annotations

from dataclasses import dataclass

from src.config import Config
from src.core.models import CouponState, Order
from src.core.store import InMemoryStore


@dataclass(frozen=True)
class ItemsPurchased:
    product_id: str
    name: str
    quantity: int


@dataclass(frozen=True)
class Report:
    orders_placed: int
    items_purchased: tuple[ItemsPurchased, ...]
    gross_minor: int
    discount_minor: int
    net_minor: int
    coupons_generated: int
    coupons_available: int
    coupons_reserved: int
    coupons_redeemed: int
    n: int
    x: int


class ReportService:
    def __init__(self, store: InMemoryStore, config: Config) -> None:
        self._store: InMemoryStore = store
        self._config: Config = config

    async def build(self) -> Report:
        """Pure read. Takes NO locks — safe because every mutation is synchronous, so
        no reader can observe a half-applied commit.  [I11][D31]"""
        placed = [order for order in self._store.orders if order.state == "PLACED"]  # [I8]
        coupons = list(self._store.coupons.values())
        return Report(
            orders_placed=len(placed),
            items_purchased=self._items_purchased(placed),
            gross_minor=sum(order.gross_minor for order in placed),
            discount_minor=sum(order.discount_minor for order in placed),
            net_minor=sum(order.net_minor for order in placed),
            coupons_generated=len(coupons),
            coupons_available=sum(1 for c in coupons if c.state is CouponState.AVAILABLE),
            coupons_reserved=sum(1 for c in coupons if c.state is CouponState.RESERVED),
            coupons_redeemed=sum(1 for c in coupons if c.state is CouponState.REDEEMED),
            n=self._config.n,
            x=self._config.x,
        )

    def _items_purchased(self, placed: list[Order]) -> tuple[ItemsPurchased, ...]:
        """Total quantity per product across PLACED orders, in seed (catalogue) order.
        Products never sold are omitted. The name is the catalogue name."""
        quantities: dict[str, int] = {}
        for order in placed:
            for line in order.lines:
                quantities[line.product_id] = quantities.get(line.product_id, 0) + line.quantity
        return tuple(
            ItemsPurchased(product_id=product.id, name=product.name, quantity=quantities[product.id])
            for product in self._store.products.values()
            if product.id in quantities
        )
