"""CheckoutService — the critical section lives here. [TAD §3.9] [TAD §4.5] [TAD §5] [TAD §6]

Shape, exactly as TAD §4.5:

    claim(cart_id, key, fingerprint)               SYNC, atomic, before any lock
    try:
        optimistic read of the cart                SYNC
        lock products(sorted) -> ledger -> cart
            re-verify cart / customer / prices     SYNC
            reserve inventory, reserve coupon      SYNC
            compute gross / discount / net         SYNC
            charge                                 <- THE ONLY YIELD POINT
            commit inventory, coupon, order, cart  SYNC, one block
        complete(cart_id, key, order_id)
    except:
        release inventory, release coupon (under the lock), release claim; re-raise

Every helper is a plain `def`. A synchronous block cannot yield, so it cannot interleave: no reader
can observe inventory decremented without the order existing. [D19]–[D25] [I1] [I2] [I3] [I5] [I13]
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from src.config import Config
from src.core.coupons import CouponService
from src.core.errors import (
    CartAlreadyCheckedOut,
    CartEmpty,
    CartModified,
    CartNotFound,
    IdempotencyKeyReused,
    PriceChanged,
    RequestInProgress,
    UnknownCustomer,
)
from src.core.idempotency import ClaimStatus, IdempotencyRegistry
from src.core.locks import LockManager
from src.core.models import Cart, CartState, Coupon, Order, OrderLine
from src.core.money import discount_minor, gross_minor, line_total_minor, net_minor
from src.core.payments import PaymentGateway
from src.core.store import InMemoryStore


@dataclass(frozen=True)
class CheckoutResult:
    order: Order
    replayed: bool  # True -> HTTP 200 + Idempotent-Replay: true  [D23]


# (quantity, unit_price_minor) per product_id — what the optimistic read saw. [TAD §4.4]
_CartSnapshot = dict[str, tuple[int, int]]


@dataclass
class _Held:
    """Exactly what the failure path must give back. [I5] [I13]"""

    inventory: list[tuple[str, int]] = field(default_factory=list)
    coupon_code: str | None = None


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
        # 1. Claim the key before any work. Synchronous insert-if-absent: atomic. [D19] [D20] [D21]
        fingerprint = IdempotencyRegistry.fingerprint(
            {"cart_id": cart_id, "customer_id": customer_id, "coupon_code": coupon_code}
        )
        claim = self._idempotency.claim(cart_id, idempotency_key, fingerprint)
        if claim.status is ClaimStatus.REPLAY:
            assert claim.order_id is not None
            return CheckoutResult(order=self._store.orders_by_id[claim.order_id], replayed=True)
        if claim.status is ClaimStatus.IN_PROGRESS:
            raise RequestInProgress(
                "A checkout with this Idempotency-Key is already in progress.",
                {"cart_id": cart_id},
            )
        if claim.status is ClaimStatus.FINGERPRINT_MISMATCH:
            raise IdempotencyKeyReused(
                "This Idempotency-Key was already used with a different request body.",
                {"cart_id": cart_id},
            )

        # 2. GRANTED. From here every exit path releases the claim. [D22]
        try:
            snapshot = self._read_snapshot(cart_id)  # optimistic, unlocked, synchronous [TAD §4.4]
            async with self._locks.acquire(
                product_ids=sorted(snapshot), coupon_ledger=True, cart_id=cart_id
            ):
                held = _Held()
                try:
                    cart = self._verify_cart_locked(cart_id, snapshot)
                    self._verify_customer_locked(customer_id)
                    lines = self._build_lines_locked(cart)
                    self._reserve_inventory_locked(lines, held)
                    coupon = self._reserve_coupon_locked(coupon_code, customer_id, held)
                    gross = gross_minor(lines)
                    discount = discount_minor(gross, coupon.percent) if coupon is not None else 0
                    net = net_minor(gross, discount)
                    order_id = f"ord_{uuid.uuid4().hex}"
                    # ---------------- THE ONLY YIELD POINT ---------------- [D25]
                    await self._payments.charge(amount_minor=net, order_ref=order_id)
                    # ------------------------------------------------------
                    order = self._commit_locked(
                        cart, customer_id, lines, coupon, order_id, gross, discount, net
                    )
                except BaseException:
                    self._release_locked(held)  # inventory, then coupon, under the lock [I5] [I13]
                    raise
        except BaseException:
            self._idempotency.release(cart_id, idempotency_key)  # [D22]
            raise

        self._idempotency.complete(cart_id, idempotency_key, order.id)
        return CheckoutResult(order=order, replayed=False)

    # --- before the lock ------------------------------------------------------------------------

    def _read_snapshot(self, cart_id: str) -> _CartSnapshot:
        """Unlocked read so we know which product locks to take. Verified again under the lock."""
        cart = self._store.carts.get(cart_id)
        if cart is None:
            raise CartNotFound("No such cart.", {"cart_id": cart_id})
        return _snapshot_of(cart)

    # --- under the lock: verification -----------------------------------------------------------

    def _verify_cart_locked(self, cart_id: str, snapshot: _CartSnapshot) -> Cart:
        """Exists, OPEN, non-empty, and identical to the optimistic read. [I2] [D8] [D13] [TAD §4.4]"""
        cart = self._store.carts.get(cart_id)
        if cart is None:
            raise CartNotFound("No such cart.", {"cart_id": cart_id})
        if cart.state is not CartState.OPEN:
            raise CartAlreadyCheckedOut("This cart has already been checked out.", {"cart_id": cart_id})
        if not cart.items:
            raise CartEmpty("The cart has no items.", {"cart_id": cart_id})
        if _snapshot_of(cart) != snapshot:
            raise CartModified(
                "The cart changed while the checkout was starting; retry.", {"cart_id": cart_id}
            )
        return cart

    def _verify_customer_locked(self, customer_id: str) -> None:
        if customer_id not in self._store.customers:
            raise UnknownCustomer("No such customer.", {"customer_id": customer_id})

    def _build_lines_locked(self, cart: Cart) -> tuple[OrderLine, ...]:
        """Every snapshot must equal the live price, or nothing proceeds. [D5] [D6] [I10]"""
        changed: list[dict[str, int | str]] = []
        lines: list[OrderLine] = []
        for item in cart.items.values():
            product = self._store.products[item.product_id]
            if item.unit_price_minor != product.unit_price_minor:
                changed.append(
                    {
                        "product_id": item.product_id,
                        "old_unit_price_minor": item.unit_price_minor,
                        "new_unit_price_minor": product.unit_price_minor,
                    }
                )
            lines.append(
                OrderLine(
                    product_id=item.product_id,
                    product_name=product.name,
                    quantity=item.quantity,
                    unit_price_minor=item.unit_price_minor,
                    line_total_minor=line_total_minor(item.unit_price_minor, item.quantity),
                )
            )
        if changed:
            raise PriceChanged(
                "One or more prices changed since the items were added; reprice the cart to accept.",
                {"changed": changed},
            )
        return tuple(lines)

    # --- under the lock: reservation --------------------------------------------------------------

    def _reserve_inventory_locked(self, lines: tuple[OrderLine, ...], held: _Held) -> None:
        """Line by line, recording each success so a mid-way refusal releases exactly the rest. [I1]"""
        for line in lines:
            self._store.reserve_inventory_locked(line.product_id, line.quantity)
            held.inventory.append((line.product_id, line.quantity))

    def _reserve_coupon_locked(self, coupon_code: str | None, customer_id: str, held: _Held) -> Coupon | None:
        if coupon_code is None:
            return None
        coupon = self._coupons.reserve_locked(coupon_code, customer_id)
        held.coupon_code = coupon.code
        return coupon

    # --- under the lock: commit, one synchronous block -------------------------------------------

    def _commit_locked(
        self,
        cart: Cart,
        customer_id: str,
        lines: tuple[OrderLine, ...],
        coupon: Coupon | None,
        order_id: str,
        gross: int,
        discount: int,
        net: int,
    ) -> Order:
        """No yield point in here: a reader sees either none of this or all of it. [D31] [I8] [I10]"""
        for line in lines:
            self._store.commit_inventory_locked(line.product_id, line.quantity)
        if coupon is not None:
            self._coupons.commit_locked(coupon.code, order_id)
        order = Order(
            id=order_id,
            sequence=len(self._store.orders) + 1,
            cart_id=cart.id,
            customer_id=customer_id,
            lines=lines,
            gross_minor=gross,
            discount_minor=discount,
            net_minor=net,
            coupon_code=coupon.code if coupon is not None else None,
            discount_percent=coupon.percent if coupon is not None else None,
            state="PLACED",
        )
        self._store.append_order_locked(order)
        cart.state = CartState.CHECKED_OUT
        return order

    # --- under the lock: failure path ------------------------------------------------------------

    def _release_locked(self, held: _Held) -> None:
        """Inventory first, then the coupon. Releases exactly what was reserved. [I5] [I13]"""
        for product_id, quantity in held.inventory:
            self._store.release_inventory_locked(product_id, quantity)
        held.inventory.clear()
        if held.coupon_code is not None:
            self._coupons.release_locked(held.coupon_code)
            held.coupon_code = None


def _snapshot_of(cart: Cart) -> _CartSnapshot:
    return {pid: (item.quantity, item.unit_price_minor) for pid, item in cart.items.items()}
