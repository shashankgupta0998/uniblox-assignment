"""ReportService: a pure, lock-free read over the store. [TAD §3.10] [D31]"""

from __future__ import annotations

from dataclasses import dataclass

from src.config import Config
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
        raise NotImplementedError
