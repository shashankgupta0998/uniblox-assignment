# TAD — Technical Architecture

**§3 signatures are FROZEN.** Sessions B and C code against them. If one is wrong, stop and report.

---

## 1. Module map and ownership

```
src/
  config.py            [shared, A creates]  Config, seed data, n, x, admin token
  core/                [Session A]
    errors.py            DomainError hierarchy + ErrorCode enum
    models.py            Product, Customer, Cart, CartItem, Order, OrderLine, Coupon
    money.py             gross / discount / net  — the only place money is computed
    store.py             InMemoryStore: dicts + the order log + the coupon ledger
    locks.py             LockManager: ordered acquisition, the deadlock-freedom guarantee
    idempotency.py       IdempotencyRegistry: atomic claim / complete / release
    payments.py          PaymentGateway protocol + FakePaymentGateway + AlwaysDeclineGateway
    carts.py             CartService
    coupons.py           CouponService
    checkout.py          CheckoutService   ← the critical section lives here
    reports.py           ReportService
  api/                 [Session B]
    schemas.py           Pydantic request/response models
    errors.py            exception handlers, envelope normalisation
    deps.py              DI: singletons, admin token guard
    routers/
      products.py  carts.py  orders.py  admin.py
  main.py              [Session B]  app factory, router wiring, OpenAPI metadata
tests/                 [Session C]
web/index.html         [Session C, optional]  demo harness  [D34]
```

**Dependency direction is one-way: `api` → `core`. `core` imports nothing from `api`.**
`core` has no FastAPI import anywhere. That is what makes `core` testable directly from a
threadpool in C9, and it is what would let the same domain sit behind a different transport.

---

## 2. Data model

All money is `int` minor units. All ids are opaque strings.

```python
@dataclass(frozen=True)
class Product:
    id: str
    name: str
    unit_price_minor: int
    stock_total: int

@dataclass(frozen=True)
class Customer:
    id: str
    name: str

@dataclass
class CartItem:
    product_id: str
    quantity: int
    unit_price_minor: int          # snapshot taken at add/update time  [D5]

class CartState(str, Enum):
    OPEN = "OPEN"
    CHECKED_OUT = "CHECKED_OUT"

@dataclass
class Cart:
    id: str
    state: CartState
    items: dict[str, CartItem]     # keyed by product_id, insertion-ordered

@dataclass(frozen=True)
class OrderLine:
    product_id: str
    product_name: str              # snapshot — order must explain itself later  [I10]
    quantity: int
    unit_price_minor: int          # snapshot
    line_total_minor: int          # quantity * unit_price_minor

@dataclass(frozen=True)
class Order:
    id: str
    sequence: int                  # monotonic from 1; milestone attribution  [D12]
    cart_id: str
    customer_id: str
    lines: tuple[OrderLine, ...]
    gross_minor: int
    discount_minor: int
    net_minor: int
    coupon_code: str | None
    discount_percent: int | None   # snapshot of x at order time
    state: Literal["PLACED"]       # the only terminal state; there is no cancel  [I8]

class CouponState(str, Enum):
    AVAILABLE = "AVAILABLE"
    RESERVED  = "RESERVED"
    REDEEMED  = "REDEEMED"

@dataclass
class Coupon:
    code: str
    percent: int                   # snapshot of x at generation time
    milestone: int                 # k; unique across all coupons  [I7]
    owner_customer_id: str         # [D12]
    state: CouponState
    redeemed_by_order_id: str | None
```

**Inventory is not stored on `Product`.** `Product.stock_total` is immutable seed data. Mutable
counters live in the store, so `I1` (`stock_total == available + reserved + sold`) is checkable
directly:

```python
store.available: dict[str, int]    # sellable right now
store.reserved:  dict[str, int]    # held by an in-flight checkout
store.sold:      dict[str, int]    # committed to orders
```

---

## 3. FROZEN INTERFACES

### 3.1 `core/errors.py`

```python
class ErrorCode(str, Enum):
    VALIDATION_FAILED          = "VALIDATION_FAILED"
    CART_NOT_FOUND             = "CART_NOT_FOUND"
    CART_EMPTY                 = "CART_EMPTY"
    CART_ALREADY_CHECKED_OUT   = "CART_ALREADY_CHECKED_OUT"
    CART_MODIFIED              = "CART_MODIFIED"
    PRODUCT_NOT_FOUND          = "PRODUCT_NOT_FOUND"
    QUANTITY_EXCEEDS_STOCK     = "QUANTITY_EXCEEDS_STOCK"
    INSUFFICIENT_INVENTORY     = "INSUFFICIENT_INVENTORY"
    PRICE_CHANGED              = "PRICE_CHANGED"
    UNKNOWN_CUSTOMER           = "UNKNOWN_CUSTOMER"
    COUPON_INVALID             = "COUPON_INVALID"
    COUPON_ALREADY_REDEEMED    = "COUPON_ALREADY_REDEEMED"
    COUPON_IN_USE              = "COUPON_IN_USE"
    NO_ELIGIBLE_MILESTONE      = "NO_ELIGIBLE_MILESTONE"
    IDEMPOTENCY_KEY_REQUIRED   = "IDEMPOTENCY_KEY_REQUIRED"
    IDEMPOTENCY_KEY_REUSED     = "IDEMPOTENCY_KEY_REUSED"
    REQUEST_IN_PROGRESS        = "REQUEST_IN_PROGRESS"
    PAYMENT_DECLINED           = "PAYMENT_DECLINED"
    ORDER_NOT_FOUND            = "ORDER_NOT_FOUND"
    FORBIDDEN                  = "FORBIDDEN"
    INTERNAL_ERROR             = "INTERNAL_ERROR"

class DomainError(Exception):
    code: ErrorCode
    http_status: int
    message: str
    details: dict[str, Any]
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None: ...
```

One concrete subclass per code, each fixing `code` and `http_status` as class attributes.
**One code ⇒ exactly one status.** Session B maps `DomainError → HTTP` generically; it never
switches on the code.

### 3.2 `core/money.py`

```python
def line_total_minor(unit_price_minor: int, quantity: int) -> int: ...
def gross_minor(lines: Iterable[OrderLine]) -> int: ...

def discount_minor(gross: int, percent: int) -> int:
    """Decimal(gross) * percent / 100, quantized to 1 with ROUND_HALF_EVEN, int(),
    then min(result, gross). Raises ValueError if percent not in [0, 100].  [D2][D3][D4]"""

def net_minor(gross: int, discount: int) -> int:
    """gross - discount. Asserts 0 <= discount <= gross.  [I9][I12]"""
```

Pure, synchronous, no I/O, no locks. The only module permitted to import `decimal`.

### 3.3 `core/locks.py`

```python
class LockManager:
    def __init__(self, product_ids: Iterable[str]) -> None: ...

    def acquire(
        self,
        *,
        product_ids: Sequence[str] = (),
        coupon_ledger: bool = False,
        cart_id: str | None = None,
    ) -> AbstractAsyncContextManager[None]:
        """Acquire in the ONE permitted order: products (sorted ascending) -> coupon
        ledger -> cart. Releases in reverse. Ordering is enforced here so callers
        cannot get it wrong. Never acquire a raw lock outside this method.  [D24]"""
```

Cart locks are created lazily per `cart_id`. There is no lock-free path to shared state.

### 3.4 `core/idempotency.py`

```python
class ClaimStatus(str, Enum):
    GRANTED              = "GRANTED"
    REPLAY               = "REPLAY"
    IN_PROGRESS          = "IN_PROGRESS"
    FINGERPRINT_MISMATCH = "FINGERPRINT_MISMATCH"

@dataclass(frozen=True)
class ClaimResult:
    status: ClaimStatus
    order_id: str | None = None

class IdempotencyRegistry:
    def claim(self, cart_id: str, key: str, fingerprint: str) -> ClaimResult:
        """SYNCHRONOUS and therefore atomic — no await, so no interleaving.
        Absent      -> insert IN_PROGRESS, return GRANTED
        IN_PROGRESS -> return IN_PROGRESS                       [D21]
        COMPLETED   -> fingerprint match: REPLAY(order_id)      [D23]
                       mismatch:          FINGERPRINT_MISMATCH  [D20]
        Note: a mismatch against an IN_PROGRESS record also returns
        FINGERPRINT_MISMATCH — checked before the in-progress check."""

    def complete(self, cart_id: str, key: str, order_id: str) -> None: ...
    def release(self, cart_id: str, key: str) -> None:
        """Drop the record so a retry after failure is a genuine new attempt.  [D22]"""

    @staticmethod
    def fingerprint(payload: Mapping[str, Any]) -> str:
        """sha256 of json.dumps(payload, sort_keys=True, separators=(',',':')).  [D20]"""
```

`claim` being synchronous is the whole mechanism. See §5.

### 3.5 `core/payments.py`

```python
@dataclass(frozen=True)
class PaymentResult:
    reference: str

class PaymentGateway(Protocol):
    async def charge(self, *, amount_minor: int, order_ref: str) -> PaymentResult:
        """Raises PaymentDeclined on failure."""

class FakePaymentGateway:
    def __init__(self, latency_seconds: float = 0.0) -> None: ...
    async def charge(self, *, amount_minor: int, order_ref: str) -> PaymentResult: ...

class AlwaysDeclineGateway:
    async def charge(self, *, amount_minor: int, order_ref: str) -> PaymentResult: ...
```

`FakePaymentGateway.charge` **must** `await asyncio.sleep(latency_seconds)` even at 0. That await is
the yield point the mutex exists to guard — without it the critical section is trivially atomic and
the concurrency design is untestable. [D25]

### 3.6 `core/store.py`

```python
class InMemoryStore:
    products: dict[str, Product]
    customers: dict[str, Customer]
    carts: dict[str, Cart]
    orders: list[Order]                 # ordered; index i -> sequence i+1
    orders_by_id: dict[str, Order]
    coupons: dict[str, Coupon]
    last_rewarded_milestone: int
    available: dict[str, int]
    reserved: dict[str, int]
    sold: dict[str, int]

    def __init__(self, config: Config) -> None: ...

    # --- synchronous, caller holds the relevant lock ---
    def reserve_inventory_locked(self, product_id: str, quantity: int) -> None:
        """Raises InsufficientInventory. available -= q; reserved += q."""
    def commit_inventory_locked(self, product_id: str, quantity: int) -> None:
        """reserved -= q; sold += q."""
    def release_inventory_locked(self, product_id: str, quantity: int) -> None:
        """reserved -= q; available += q.  [I13]"""
    def append_order_locked(self, order: Order) -> None: ...

    # --- lock-free reads; safe because every mutation is synchronous ---
    def placed_order_count(self) -> int: ...
    def available_quantity(self, product_id: str) -> int: ...
```

### 3.7 `core/carts.py`

```python
@dataclass(frozen=True)
class CartLineView:
    product_id: str
    product_name: str
    quantity: int
    unit_price_minor: int
    line_total_minor: int
    current_unit_price_minor: int     # live price, so drift is visible before checkout
    price_changed: bool

@dataclass(frozen=True)
class CartView:
    id: str
    state: CartState
    lines: tuple[CartLineView, ...]
    gross_minor: int
    has_price_changes: bool

class CartService:
    def __init__(self, store: InMemoryStore, locks: LockManager) -> None: ...
    async def create_cart(self) -> CartView: ...
    async def get_cart(self, cart_id: str) -> CartView: ...
    async def add_item(self, cart_id: str, product_id: str, quantity: int) -> CartView: ...
    async def set_item_quantity(self, cart_id: str, product_id: str, quantity: int) -> CartView: ...
    async def remove_item(self, cart_id: str, product_id: str) -> CartView: ...
    async def reprice(self, cart_id: str) -> CartView:
        """Re-snapshot every line to the current price.  [D6]"""
```

All mutators acquire `products=[product_id]` then `cart_id`, via `LockManager`. Never cart-first.

### 3.8 `core/coupons.py`

```python
@dataclass(frozen=True)
class GenerationResult:
    coupon: Coupon
    pending_milestones: int

class CouponService:
    def __init__(self, store: InMemoryStore, locks: LockManager, config: Config) -> None: ...

    async def generate(self) -> GenerationResult:
        """Admin. Acquires the coupon ledger lock itself. Rewards the lowest unrewarded
        reached milestone k = last_rewarded + 1, eligible iff placed_orders >= k*n.
        Binds to store.orders[k*n - 1].customer_id. Raises NoEligibleMilestone.
        [D12][D14][I7]"""

    async def list_coupons(self) -> tuple[Coupon, ...]: ...

    # --- synchronous; caller MUST hold the coupon ledger lock ---
    def reserve_locked(self, code: str, customer_id: str) -> Coupon:
        """Unknown code, or owner != customer_id -> CouponInvalid (identical).  [D17][I6]
        REDEEMED and owned by caller -> CouponAlreadyRedeemed.
        RESERVED -> CouponInUse.  [I4]
        AVAILABLE -> set RESERVED, return it."""
    def commit_locked(self, code: str, order_id: str) -> None: ...
    def release_locked(self, code: str) -> None:
        """RESERVED -> AVAILABLE.  [I5]"""
```

`reserve/commit/release` are synchronous and non-locking because the caller (`CheckoutService`)
already holds the ledger lock. `asyncio.Lock` is not reentrant — a self-locking variant would
deadlock instantly.

### 3.9 `core/checkout.py`

```python
@dataclass(frozen=True)
class CheckoutResult:
    order: Order
    replayed: bool          # True -> HTTP 200 + Idempotent-Replay: true  [D23]

class CheckoutService:
    def __init__(
        self,
        store: InMemoryStore,
        locks: LockManager,
        idempotency: IdempotencyRegistry,
        coupons: CouponService,
        payments: PaymentGateway,
        config: Config,
    ) -> None: ...

    async def checkout(
        self,
        *,
        cart_id: str,
        customer_id: str,
        coupon_code: str | None,
        idempotency_key: str,
    ) -> CheckoutResult:
        """The entire critical section. See §4 and §5."""
```

The fingerprint is computed **inside** `checkout` from `{cart_id, customer_id, coupon_code}` so
Session B cannot get it wrong and cannot drift from Session A.

### 3.10 `core/reports.py`

```python
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
    def __init__(self, store: InMemoryStore, config: Config) -> None: ...
    async def build(self) -> Report:
        """Pure read. Takes NO locks — safe because every mutation is synchronous, so
        no reader can observe a half-applied commit.  [I11][D31]"""
```

### 3.11 `config.py` — shared, A creates in A1

```python
@dataclass(frozen=True)
class Config:
    n: int = 5
    x: int = 10
    admin_token: str = "dev-admin-token"
    payment_latency_seconds: float = 0.0
    currency: str = "INR"

SEED_PRODUCTS: tuple[Product, ...]      # the six in PRD 2.1
SEED_CUSTOMERS: tuple[Customer, ...]    # cus_1..cus_5

def load_config() -> Config:
    """Env overrides: REWARD_N, REWARD_X, ADMIN_TOKEN, PAYMENT_LATENCY_SECONDS.  [D35]"""
```

---

## 4. Concurrency strategy

### 4.1 The execution model, precisely

FastAPI runs `async def` handlers on a single event loop. **There is no preemption inside a
synchronous block** — two coroutines cannot interleave between two adjacent synchronous statements.
Interleaving happens **only at `await`**. Verified empirically before this design was fixed:

| Scenario | Result |
|---|---|
| 10 coroutines × 10,000 synchronous decrements | 0 lost updates — atomic by construction |
| 100 coroutines, `await` inside the critical section, **no lock** | **oversold 99 of 100** |
| Same, with `asyncio.Lock` spanning the await | 0 lost updates |
| 100 calls via `ThreadPoolExecutor`, blocking I/O in section, no lock | **oversold 95 of 100** |

So the mutex is not ceremony: **it is load-bearing precisely because the payment call is awaited
inside the critical section.** Remove `asyncio.Lock` and test C3 fails. [D24] [D25]

### 4.2 What locks exist

| Lock | Protects | Class order |
|---|---|---|
| `product[id]` — one per product | `available` / `reserved` / `sold` for that product. **I1** | 1 (ascending id) |
| `coupon_ledger` — one, global | all coupon states, `last_rewarded_milestone`, the order log. **I4 I5 I7** | 2 |
| `cart[id]` — one per cart | cart items and `CartState`. **I2** | 3 |

Per-product rather than one global lock so two checkouts for **different** products genuinely
proceed concurrently — otherwise the design serialises everything and proves nothing.

### 4.3 Acquisition order and why

```
products (ascending id)  ->  coupon ledger  ->  cart
```

A **total order over lock classes** makes deadlock structurally impossible: a cycle requires two
holders each waiting on a lock the other holds, which requires at least one of them to acquire out
of order. Sorting product ids handles the intra-class case — two checkouts touching
`{dock, headset}` in opposite cart order would otherwise deadlock. `LockManager.acquire` enforces
the order by construction, so it cannot be violated by a caller.

### 4.4 The chicken-and-egg, and `CART_MODIFIED`

We must know the cart's products to lock them, but reading the cart *should* be under its lock —
which comes last. Resolved with optimistic read-then-verify:

1. Read the cart's product ids **without** locks (a synchronous read, therefore atomic).
2. Acquire `products(sorted) → ledger → cart`.
3. **Re-read the cart under lock.** If its product-id set changed, release everything and raise
   `409 CART_MODIFIED`.

The window is narrow (another request adding an item between steps 1 and 2) but real, so it is
handled explicitly rather than assumed away.

### 4.5 The checkout critical section — exactly one `await`

```
claim(cart_id, key, fingerprint)              SYNC, atomic       <- before any lock
  not GRANTED -> return replay / 409
try:
  async with locks.acquire(products=sorted(ids), coupon_ledger=True, cart_id=cart_id):
      verify cart exists / OPEN / non-empty / unchanged          SYNC
      verify customer exists                                     SYNC
      verify every price snapshot == live price                  SYNC   -> 409 PRICE_CHANGED
      reserve_inventory_locked(...) for each line                SYNC   -> 409 INSUFFICIENT_INVENTORY
      coupons.reserve_locked(code, customer_id)                  SYNC   -> 422/409
      compute gross / discount / net                             SYNC
      # ---------------- THE ONLY AWAIT ----------------
      await payments.charge(...)                                        -> 402 PAYMENT_DECLINED
      # ------------------------------------------------
      commit_inventory_locked(...) for each line                 SYNC
      coupons.commit_locked(code, order_id)                      SYNC
      append_order_locked(order)                                 SYNC
      cart.state = CHECKED_OUT                                   SYNC
  idempotency.complete(cart_id, key, order.id)
except:
  release reserved inventory + coupon (SYNC)
  idempotency.release(cart_id, key)                              [D22]
  raise
```

The commit block is **one synchronous run** — no reader can observe inventory decremented without
the order existing. That is why `ReportService` needs no locks. [D31]

---

## 5. Idempotency strategy

**Key source.** The `Idempotency-Key` request header, client-generated. Required on checkout;
missing → `400 IDEMPOTENCY_KEY_REQUIRED`.

**Scope.** `(cart_id, key)`. [D19] The operation being made idempotent is "check out cart X", so the
key is scoped to the resource it acts on. This also makes lazy client keys *safe*: a client sending
`Idempotency-Key: 1` on every checkout is fine, because each checkout targets a different cart.
Under `(customer_id, key)` that client's second order would collide with its first and silently
receive the **wrong order**; under a global scope it would receive **another customer's order**.

**Storage shape.**

```python
{(cart_id, key): IdempotencyRecord(
     state: "IN_PROGRESS" | "COMPLETED",
     fingerprint: str,          # sha256 of canonical {cart_id, customer_id, coupon_code}
     order_id: str | None,
)}
```

**The concurrent retry — the case that actually matters.** The naive implementation ("look up; if
absent, run; then store") is broken, because during the payment await the first request has stored
nothing:

```
t=0    R1 (key K) arrives.  Lookup: ABSENT.  Proceeds.
t=1    R1 reserves inventory + coupon.
t=2    R1 awaits payment.  <-- YIELDS
t=3    R2 (key K) arrives.  Lookup: ABSENT (R1 has stored nothing yet).  Proceeds.
t=4    R2 reserves inventory AGAIN.
=>     two orders, inventory charged twice
```

The fix is to **claim the key before doing the work**, with a synchronous insert-if-absent. Because
`claim` contains no `await`, the check-and-insert cannot be interleaved — the event loop *is* the
mutex for that one function, and no lock is needed for it. R2's claim then finds `IN_PROGRESS`.

**R2's behaviour: `409 REQUEST_IN_PROGRESS`.** [D21] Chosen over joining the in-flight promise
because (a) joining pins every retry in server memory awaiting one slow gateway, converting retries
into memory pressure, (b) joining forces a definition of what the joiner receives when the original
*rejects*, a subtle branch with real bug potential, and (c) the fail-fast contract composes cleanly
with replay: `409 REQUEST_IN_PROGRESS` → client backs off → retries → original has completed →
`200` + `Idempotent-Replay: true`.

**Different body, same key → `409 IDEMPOTENCY_KEY_REUSED`.** [D20] Checked *before* the
in-progress check, so a mismatched replay is never mistaken for a legitimate retry. The motivating
case is money: first request carries a coupon, the retry omits it. Replay-and-ignore would return a
*discounted* order to a caller that asked for no discount — and the reverse silently drops a coupon
the caller believes was applied. Both are silent wrong-money outcomes.

**Failure handling: successes only.** [D22] The record is released on any failure. Caching failures
poisons the key — a client that gets `409 PRICE_CHANGED`, calls `reprice`, and retries with the same
key would receive the cached `PRICE_CHANGED` forever and would have to mint a new key to recover
from a problem the server told it to fix. The key guarantees *at most one order*, not a frozen
response for all time. That is a deliberate departure from strict replay semantics.

**No TTL and no eviction.** Records live for process lifetime. In production this is a table with a
retention policy; see §9.

---

## 6. Reservation lifecycle

### 6.1 Inventory

```
                 reserve_inventory_locked          commit_inventory_locked
  available  ─────────────────────────────>  reserved  ──────────────────────>  sold
      ^                                          │
      └──────────────────────────────────────────┘
                 release_inventory_locked   (any failure after reserve)   [I13]
```

`stock_total == available + reserved + sold` holds at every observable moment. **I1**
Reservations exist only for the duration of one checkout — there is no timeout, because there is no
path that abandons a reservation: the `try/except` releases on every exit. Add-to-cart reserves
nothing. [D7]

### 6.2 Coupons

```
                        reserve_locked                     commit_locked
   (generate) ──> AVAILABLE ──────────> RESERVED ─────────────────────> REDEEMED
                      ^                    │                            (terminal)
                      └────────────────────┘
                          release_locked   (any failure after reserve)   [I5]
```

`generated == available + reserved + redeemed` at every moment. **I15** — which is exactly why the
report exposes `reserved`: hiding it would make the identity appear to break during an in-flight
checkout. [D31]

Both releases are in the same `except` block as the inventory release, so "coupon lost by a failed
checkout" and "inventory lost by a failed checkout" are one code path with one test each.

---

## 7. Error catalogue

| Code | Status | Meaning | Invariant |
|---|---|---|---|
| `VALIDATION_FAILED` | 422 | Schema violation. Normalised from Pydantic. | — |
| `CART_NOT_FOUND` | 404 | Unknown cart id | — |
| `CART_EMPTY` | 422 | Checkout of a cart with no lines [D13] | — |
| `CART_ALREADY_CHECKED_OUT` | 409 | Second checkout, or mutation after checkout | I2 |
| `CART_MODIFIED` | 409 | Cart changed between the unlocked read and lock acquisition (§4.4) | — |
| `PRODUCT_NOT_FOUND` | 404 | Unknown product id | — |
| `QUANTITY_EXCEEDS_STOCK` | 422 | Add/update above current stock. **Advisory.** [D7] [D9] | — |
| `INSUFFICIENT_INVENTORY` | 409 | Lost the race at checkout. **Contention, not client error.** [D9] | I1 |
| `PRICE_CHANGED` | 409 | Snapshot ≠ live price. `details.changed[]` enumerates lines. [D5] | I10 |
| `UNKNOWN_CUSTOMER` | 422 | `customer_id` not in the seeded set | — |
| `COUPON_INVALID` | 422 | Nonexistent **or** owned by another customer. Identical body. [D17] | I6 |
| `COUPON_ALREADY_REDEEMED` | 422 | Redeemed, and owned by the requesting customer | I4 |
| `COUPON_IN_USE` | 409 | Reserved by a concurrent checkout | I4 |
| `NO_ELIGIBLE_MILESTONE` | 409 | Admin generate with nothing to reward [D14] | I7 |
| `IDEMPOTENCY_KEY_REQUIRED` | 400 | Missing header on checkout | I3 |
| `IDEMPOTENCY_KEY_REUSED` | 409 | Same key, different body [D20] | I3 |
| `REQUEST_IN_PROGRESS` | 409 | Concurrent replay of an in-flight key [D21] | I3 |
| `PAYMENT_DECLINED` | 402 | Gateway rejected. Everything released. [D25] | I5 I13 |
| `ORDER_NOT_FOUND` | 404 | Unknown order id | — |
| `FORBIDDEN` | 403 | Missing or wrong `X-Admin-Token` [D28] | — |
| `INTERNAL_ERROR` | 500 | Unhandled. Never leaks a stack trace. | — |

Envelope: `{"error": {"code": ..., "message": ..., "details": {...}}}`. `details` is `{}` unless the
table says otherwise. **Distinguishability is by cause the caller can act on** — which is why
"not your coupon" is deliberately indistinguishable from "no such coupon". [D17]

---

## 8. HTTP surface

| Method | Path | Success | Notes |
|---|---|---|---|
| GET | `/products` | 200 | id, name, price, available |
| GET | `/customers` | 200 | seeded ids, for the demo harness |
| POST | `/carts` | 201 | no customer attached [D11] |
| GET | `/carts/{cart_id}` | 200 | includes `price_changed` per line |
| POST | `/carts/{cart_id}/items` | 201 | `{product_id, quantity}` |
| PUT | `/carts/{cart_id}/items/{product_id}` | 200 | `{quantity}` absolute |
| DELETE | `/carts/{cart_id}/items/{product_id}` | 204 | |
| POST | `/carts/{cart_id}/reprice` | 200 | re-snapshot [D6] |
| POST | `/carts/{cart_id}/checkout` | **201** / **200** replay | `Idempotency-Key` required |
| GET | `/orders/{order_id}` | 200 | |
| POST | `/admin/coupons` | 201 | **admin** |
| GET | `/admin/coupons` | 200 | **admin** |
| GET | `/admin/report` | 200 | **admin**, pure read |

OpenAPI is generated by FastAPI from the Pydantic models — it *is* the API documentation
deliverable. `/docs` serves Swagger UI. [D27]

---

## 9. Multiple instances and a real database

Every in-process construct maps to a production equivalent. **The headline: with a real database and
one transaction per checkout, most of the reservation machinery collapses into `ROLLBACK`.** The
in-memory design is *more* complex than the production one, not less, because it has no transactions.

| In-process | Production equivalent | Note |
|---|---|---|
| `asyncio.Lock` per product | `SELECT … FOR UPDATE` on the product row, or better an atomic conditional write: `UPDATE products SET available = available - :q WHERE id = :id AND available >= :q` and check rowcount | The conditional write needs no lock at all and is the preferred form |
| `coupon_ledger` lock | `UPDATE coupons SET state='RESERVED' WHERE code=:c AND state='AVAILABLE'` — rowcount 1 wins the race, 0 loses | Compare-and-swap replaces mutual exclusion. **I4** falls out of the `WHERE` clause |
| Milestone uniqueness | `UNIQUE(milestone)` on `coupons` | **I7** becomes a constraint violation, not application logic |
| `cart[id]` lock | Row lock, or an optimistic `version` column with `WHERE version = :v` | Optimistic is better: carts are low-contention |
| `IdempotencyRegistry` dict | Table with `UNIQUE(cart_id, key)`. `INSERT` claims it; a unique violation means duplicate | The DB's unique constraint replaces the synchronous-function atomicity |
| `claim` returning `IN_PROGRESS` | The row exists with `state='IN_PROGRESS'` and no `order_id` | Needs a **stale-claim TTL**: an instance that crashes mid-checkout leaves the key claimed forever. In-process this cannot happen (the process holding it is the process that died) |
| `release()` on failure | `ROLLBACK` — the whole record vanishes | Simpler than the in-memory version |
| Inventory reservations | Unnecessary within a single transaction. Needed again only if payment moves *outside* the transaction — then it is a saga with a compensating release and a reaper for expired reservations | This is the real design fork at scale |
| Lock ordering rule | **Still required.** Row locks deadlock too. Postgres detects and aborts one transaction, so add retry-on-deadlock (`40P01`) | The rule survives the migration; the enforcement mechanism does not |
| `store.orders` list | `orders` table; `sequence` from a sequence or `count(*) WHERE state='PLACED'` inside the transaction | A cached counter across instances is a correctness bug |
| `ReportService` lock-free read | `REPEATABLE READ` transaction, or accept read-committed skew | The "synchronous commit ⇒ consistent read" argument does **not** survive multiple processes |
| Single-process event loop | N stateless instances behind a load balancer; **all** shared state moves to the DB | No in-process state may survive this migration except caches |

**What breaks first at multi-instance.** In order: (1) `IdempotencyRegistry` — a second instance
knows nothing about the first's in-flight key, so duplicate orders return immediately; (2)
`last_rewarded_milestone` — two admin calls on two instances both reward milestone k; (3) the
product locks. Items 1 and 2 are *silent* correctness failures, item 3 is loud. That ordering is why
idempotency and the coupon ledger are the two things to move to the database first.

---

## 10. What this architecture deliberately does not have

No repository abstraction over the store — a one-implementation interface is speculative generality
at this size. `InMemoryStore` is the seam; swapping it means writing a real one, and §9 says what it
would look like. No event bus, no CQRS, no caching layer, no background workers, no retry
middleware. [D33]
