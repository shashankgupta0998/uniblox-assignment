"""CheckoutService — the critical section lives here. [TAD §3.9] [TAD §4.5] [TAD §5]"""

from __future__ import annotations

from dataclasses import dataclass

from src.config import Config
from src.core.coupons import CouponService
from src.core.idempotency import IdempotencyRegistry
from src.core.locks import LockManager
from src.core.models import Order
from src.core.payments import PaymentGateway
from src.core.store import InMemoryStore


@dataclass(frozen=True)
class CheckoutResult:
    order: Order
    replayed: bool  # True -> HTTP 200 + Idempotent-Replay: true  [D23]


class CheckoutService:
    def __init__(
        self,
        store: InMemoryStore,
        locks: LockManager,
        idempotency: IdempotencyRegistry,
        coupons: CouponService,
        payments: PaymentGateway,
        config: Config,
    ) -> None:
        self._store: InMemoryStore = store
        self._locks: LockManager = locks
        self._idempotency: IdempotencyRegistry = idempotency
        self._coupons: CouponService = coupons
        self._payments: PaymentGateway = payments
        self._config: Config = config

    async def checkout(
        self,
        *,
        cart_id: str,
        customer_id: str,
        coupon_code: str | None,
        idempotency_key: str,
    ) -> CheckoutResult:
        """The entire critical section. See §4 and §5."""
        raise NotImplementedError
