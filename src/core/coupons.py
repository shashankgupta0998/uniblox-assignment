"""CouponService: milestone generation and the ledger transitions. [TAD §3.8] [TAD §6.2]

Reward rule (PRD §2.5): milestone k is reached when placed >= k*n and rewarded at most once, ever,
with one coupon bound to the customer who placed order number k*n. [I7] [D12] [D14] [D15]

Ledger: AVAILABLE -> RESERVED -> REDEEMED, RESERVED -> AVAILABLE on failure. [D30]
`generated == available + reserved + redeemed` at every moment. [I15]

`reserve_locked` / `commit_locked` / `release_locked` are synchronous and take no lock: the caller
(CheckoutService) already holds the coupon ledger lock, and asyncio.Lock is not reentrant.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from src.config import Config
from src.core.errors import CouponAlreadyRedeemed, CouponInUse, CouponInvalid, NoEligibleMilestone
from src.core.locks import LockManager
from src.core.models import Coupon, CouponState
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
        async with self._locks.acquire(coupon_ledger=True):
            return self._generate_locked()

    async def list_coupons(self) -> tuple[Coupon, ...]:
        """Lock-free read, creation order."""
        return tuple(self._store.coupons.values())

    # --- synchronous; caller MUST hold the coupon ledger lock ---
    def reserve_locked(self, code: str, customer_id: str) -> Coupon:
        """Unknown code, or owner != customer_id -> CouponInvalid (identical).  [D17][I6]
        REDEEMED and owned by caller -> CouponAlreadyRedeemed.
        RESERVED -> CouponInUse.  [I4]
        AVAILABLE -> set RESERVED, return it."""
        coupon = self._store.coupons.get(code)
        if coupon is None or coupon.owner_customer_id != customer_id:
            raise _coupon_invalid()
        if coupon.state is CouponState.REDEEMED:
            raise CouponAlreadyRedeemed(
                "This coupon has already been redeemed.",
                {"coupon_code": code, "redeemed_by_order_id": coupon.redeemed_by_order_id},
            )
        if coupon.state is CouponState.RESERVED:
            raise CouponInUse("This coupon is being redeemed by another checkout.", {"coupon_code": code})
        coupon.state = CouponState.RESERVED
        return coupon

    def commit_locked(self, code: str, order_id: str) -> None:
        """RESERVED -> REDEEMED, recording the one order that redeemed it. [I4]"""
        coupon = self._require_state_locked(code, CouponState.RESERVED)
        coupon.state = CouponState.REDEEMED
        coupon.redeemed_by_order_id = order_id

    def release_locked(self, code: str) -> None:
        """RESERVED -> AVAILABLE.  [I5]"""
        coupon = self._require_state_locked(code, CouponState.RESERVED)
        coupon.state = CouponState.AVAILABLE

    # --- internals ----------------------------------------------------------------------------

    def _generate_locked(self) -> GenerationResult:
        """Under the ledger lock. One synchronous block: read the count, mint, record. [I7]"""
        n = self._config.n
        k = self._store.last_rewarded_milestone + 1
        placed = self._store.placed_order_count()
        if placed < k * n:
            raise NoEligibleMilestone(
                "No milestone has been reached that is not already rewarded.",
                {"placed_orders": placed, "next_milestone_at": k * n},
            )
        coupon = Coupon(
            code=_new_code(),
            percent=self._config.x,
            milestone=k,
            owner_customer_id=self._store.orders[k * n - 1].customer_id,
            state=CouponState.AVAILABLE,
            redeemed_by_order_id=None,
        )
        self._store.coupons[coupon.code] = coupon
        self._store.last_rewarded_milestone = k
        return GenerationResult(coupon=coupon, pending_milestones=placed // n - k)

    def _require_state_locked(self, code: str, expected: CouponState) -> Coupon:
        """A transition from any other state is a caller bug, never a client error."""
        coupon = self._store.coupons.get(code)
        if coupon is None:
            raise ValueError(f"unknown coupon code {code!r}")
        if coupon.state is not expected:
            raise ValueError(f"coupon {code!r} is {coupon.state.value}, expected {expected.value}")
        return coupon


def _new_code() -> str:
    """uuid4-derived and opaque: never milestone-derived, never guessable. [D37]"""
    return uuid.uuid4().hex[:16]


def _coupon_invalid() -> CouponInvalid:
    """The single source of the COUPON_INVALID error, so unknown-code and wrong-owner are
    byte-identical: nothing in message or details reveals that another customer's coupon exists."""
    return CouponInvalid("This coupon cannot be applied.", {})
