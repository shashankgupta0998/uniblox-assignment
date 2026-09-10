"""CouponService: milestone generation and the ledger transitions. [TAD §3.8] [TAD §6.2]"""

from __future__ import annotations

from dataclasses import dataclass

from src.config import Config
from src.core.locks import LockManager
from src.core.models import Coupon
from src.core.store import InMemoryStore


@dataclass(frozen=True)
class GenerationResult:
    coupon: Coupon
    pending_milestones: int


class CouponService:
    def __init__(self, store: InMemoryStore, locks: LockManager, config: Config) -> None:
        self._store: InMemoryStore = store
        self._locks: LockManager = locks
        self._config: Config = config

    async def generate(self) -> GenerationResult:
        """Admin. Acquires the coupon ledger lock itself. Rewards the lowest unrewarded
        reached milestone k = last_rewarded + 1, eligible iff placed_orders >= k*n.
        Binds to store.orders[k*n - 1].customer_id. Raises NoEligibleMilestone.
        [D12][D14][I7]"""
        raise NotImplementedError

    async def list_coupons(self) -> tuple[Coupon, ...]:
        raise NotImplementedError

    # --- synchronous; caller MUST hold the coupon ledger lock ---
    def reserve_locked(self, code: str, customer_id: str) -> Coupon:
        """Unknown code, or owner != customer_id -> CouponInvalid (identical).  [D17][I6]
        REDEEMED and owned by caller -> CouponAlreadyRedeemed.
        RESERVED -> CouponInUse.  [I4]
        AVAILABLE -> set RESERVED, return it."""
        raise NotImplementedError

    def commit_locked(self, code: str, order_id: str) -> None:
        raise NotImplementedError

    def release_locked(self, code: str) -> None:
        """RESERVED -> AVAILABLE.  [I5]"""
        raise NotImplementedError
