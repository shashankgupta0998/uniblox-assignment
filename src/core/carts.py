"""CartService and its view objects. [TAD §3.7] [D5] [D6] [D7] [D8] [D37]

Every mutator: validate the cart and the product exist (no locks needed for an existence check on
immutable seed data and a synchronous dict read), then acquire `products=[product_id]` -> `cart_id`
through the LockManager, then mutate synchronously under the lock. Never cart-first, never a raw
lock. Add-to-cart reserves nothing: the stock check is advisory. [D7]
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from src.core.errors import CartAlreadyCheckedOut, CartNotFound, ProductNotFound, QuantityExceedsStock
from src.core.locks import LockManager
from src.core.models import Cart, CartItem, CartState, OrderLine, Product
from src.core.money import gross_minor, line_total_minor
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
        """A fresh OPEN cart with an opaque uuid4-derived id. No customer attached. [D11] [D37]"""
        cart = Cart(id=f"crt_{uuid.uuid4().hex}", state=CartState.OPEN, items={})
        self._store.carts[cart.id] = cart
        return self._view(cart)

    async def get_cart(self, cart_id: str) -> CartView:
        """Lock-free read: a synchronous view build cannot observe a half-applied mutation."""
        return self._view(self._require_cart(cart_id))

    async def add_item(self, cart_id: str, product_id: str, quantity: int) -> CartView:
        """New line, or increment an existing one. Either way the price is re-snapshotted. [D5]"""
        self._require_cart(cart_id)
        self._require_product(product_id)
        async with self._locks.acquire(product_ids=[product_id], cart_id=cart_id):
            cart = self._require_open_cart_locked(cart_id)
            existing = cart.items.get(product_id)
            total = quantity + (existing.quantity if existing is not None else 0)
            self._set_line_locked(cart, product_id, total)
            return self._view(cart)

    async def set_item_quantity(self, cart_id: str, product_id: str, quantity: int) -> CartView:
        """Absolute quantity, re-snapshotted. Creates the line if it is not in the cart yet."""
        self._require_cart(cart_id)
        self._require_product(product_id)
        async with self._locks.acquire(product_ids=[product_id], cart_id=cart_id):
            cart = self._require_open_cart_locked(cart_id)
            self._set_line_locked(cart, product_id, quantity)
            return self._view(cart)

    async def remove_item(self, cart_id: str, product_id: str) -> CartView:
        """Drop the line. Removing a line that is not in the cart is a no-op (idempotent delete)."""
        self._require_cart(cart_id)
        self._require_product(product_id)
        async with self._locks.acquire(product_ids=[product_id], cart_id=cart_id):
            cart = self._require_open_cart_locked(cart_id)
            cart.items.pop(product_id, None)
            return self._view(cart)

    async def reprice(self, cart_id: str) -> CartView:
        """Re-snapshot every line to the current price.  [D6]"""
        cart = self._require_cart(cart_id)
        product_ids = list(cart.items)  # the manager sorts; an item added meanwhile is re-read under lock
        async with self._locks.acquire(product_ids=product_ids, cart_id=cart_id):
            cart = self._require_open_cart_locked(cart_id)
            for item in cart.items.values():
                item.unit_price_minor = self._store.products[item.product_id].unit_price_minor
            return self._view(cart)

    # --- guards -----------------------------------------------------------------------------

    def _require_cart(self, cart_id: str) -> Cart:
        cart = self._store.carts.get(cart_id)
        if cart is None:
            raise CartNotFound("No such cart.", {"cart_id": cart_id})
        return cart

    def _require_product(self, product_id: str) -> Product:
        product = self._store.products.get(product_id)
        if product is None:
            raise ProductNotFound("No such product.", {"product_id": product_id})
        return product

    def _require_open_cart_locked(self, cart_id: str) -> Cart:
        """Under the cart lock: exists and is OPEN. A CHECKED_OUT cart rejects every mutator. [I2] [D8]"""
        cart = self._require_cart(cart_id)
        if cart.state is not CartState.OPEN:
            raise CartAlreadyCheckedOut("This cart has already been checked out.", {"cart_id": cart_id})
        return cart

    # --- mutation, under the lock ----------------------------------------------------------

    def _set_line_locked(self, cart: Cart, product_id: str, quantity: int) -> None:
        """Advisory stock check against live `available`, then snapshot the live price. [D5] [D7] [D9]"""
        available = self._store.available[product_id]
        if quantity > available:
            raise QuantityExceedsStock(
                "Requested quantity exceeds current stock.",
                {"product_id": product_id, "requested": quantity, "available": available},
            )
        live_price = self._store.products[product_id].unit_price_minor
        cart.items[product_id] = CartItem(product_id=product_id, quantity=quantity, unit_price_minor=live_price)

    # --- views -------------------------------------------------------------------------------

    def _view(self, cart: Cart) -> CartView:
        lines = tuple(self._line_view(item) for item in cart.items.values())
        order_lines = [
            OrderLine(line.product_id, line.product_name, line.quantity, line.unit_price_minor, line.line_total_minor)
            for line in lines
        ]
        return CartView(
            id=cart.id,
            state=cart.state,
            lines=lines,
            gross_minor=gross_minor(order_lines),
            has_price_changes=any(line.price_changed for line in lines),
        )

    def _line_view(self, item: CartItem) -> CartLineView:
        product = self._store.products[item.product_id]
        return CartLineView(
            product_id=item.product_id,
            product_name=product.name,
            quantity=item.quantity,
            unit_price_minor=item.unit_price_minor,
            line_total_minor=line_total_minor(item.unit_price_minor, item.quantity),
            current_unit_price_minor=product.unit_price_minor,
            price_changed=item.unit_price_minor != product.unit_price_minor,
        )
