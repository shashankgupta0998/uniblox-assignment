# PRD — Reliable Checkout and Rewards Service

Scope: the backend defined in `be/README.md`. Decisions are tagged `[D#]` and expanded in `DECISIONS.md`.

---

## 1. Actors

| Actor | Identity model | How it is nominally marked |
|---|---|---|
| **Customer** | One of 5 seeded ids (`cus_1`…`cus_5`). Supplied at checkout, never validated as a credential. [D10] | `customer_id` in the checkout request body [D11] |
| **Administrator** | No identity. A shared placeholder token. [D28] | `/admin/*` path prefix **and** `X-Admin-Token` header |

Auth is an explicit non-goal. Both markers are labels, not access control. See `SAD.md` §1.

### Administrative operations

These three, and only these:

1. `POST /admin/coupons` — generate a coupon for an eligible, unrewarded milestone
2. `GET /admin/report` — the reconciliation summary
3. `GET /admin/coupons` — list coupons and their states (supports the report and the demo harness)

Everything else is a customer operation. `GET /products` and `GET /customers` are unauthenticated
read-only helpers for the demo harness.

---

## 2. Capabilities and acceptance criteria

### 2.1 Products

Seeded at startup, immutable through the API. At least five, at least one scarce.

| id | name | unit price | stock |
|---|---|---|---|
| `prd_keyboard` | Mechanical Keyboard | ₹4,499.00 | 25 |
| `prd_mouse` | Wireless Mouse | ₹1,299.00 | 40 |
| `prd_monitor` | 27" Monitor | ₹18,999.00 | 10 |
| `prd_cable` | USB-C Cable | ₹399.00 | 100 |
| `prd_dock` | Thunderbolt Dock | ₹24,999.00 | **3** ← scarce, used for oversell tests |
| `prd_headset` | Noise-Cancelling Headset | ₹8,999.00 | **1** ← scarce, used for coupon/oversell races |

**AC-P1** `GET /products` returns id, name, `unit_price_minor`, and *available* quantity.
**AC-P2** Prices are integer minor units. No float appears in any response. **I14**

### 2.2 Carts

**AC-C1** `POST /carts` creates an empty cart and returns its id. No customer is attached. [D11]
**AC-C2** `GET /carts/{id}` returns each line with `product_id`, `quantity`, `unit_price_minor` (the
snapshot taken at add time [D5]), `line_total_minor`, and a cart `gross_minor`.
**AC-C3** `POST /carts/{id}/items` adds a line. Quantity must be an integer in `[1, 100]`. [D32]
Adding a product already in the cart **increments** its quantity and re-snapshots the price.
**AC-C4** `PUT /carts/{id}/items/{product_id}` sets an absolute quantity and re-snapshots the price.
**AC-C5** `DELETE /carts/{id}/items/{product_id}` removes the line. `204`.
**AC-C6** An unknown product → `404 PRODUCT_NOT_FOUND`. A quantity above current stock →
`422 QUANTITY_EXCEEDS_STOCK`. A zero, negative, or non-integer quantity → `422 VALIDATION_FAILED`.
No invalid line ever enters the cart.
**AC-C7** The add-time stock check is **advisory only**. It reserves nothing. Ten carts may each
hold the last unit; exactly one wins at checkout. [D7]
**AC-C8** A cart is checked out at most once. Any further checkout, item mutation, or reprice on a
`CHECKED_OUT` cart → `409 CART_ALREADY_CHECKED_OUT`. **I2** [D8]
**AC-C9** `POST /carts/{id}/reprice` refreshes every line's price snapshot to the current price and
returns the updated cart. This is the customer explicitly accepting a price change. [D6]

### 2.3 Checkout

**AC-K1** `POST /carts/{id}/checkout` requires an `Idempotency-Key` header. Missing → `400
IDEMPOTENCY_KEY_REQUIRED`. Body: `{"customer_id": "...", "coupon_code": "..."|null}`.
**AC-K2** An empty cart → `422 CART_EMPTY`. No zero-line order is ever created. [D13]
**AC-K3** If any line's snapshot price differs from the product's current price → `409
PRICE_CHANGED`, with a body enumerating `{product_id, old_unit_price_minor, new_unit_price_minor}`
for every changed line. No order, no reservation, nothing consumed. [D5] [D6]
**AC-K4** If any line exceeds available stock at checkout time → `409 INSUFFICIENT_INVENTORY`.
Distinct from the add-time `422 QUANTITY_EXCEEDS_STOCK`: this one is contention, not a client error. [D9]
**AC-K5** Concurrent checkouts never oversell. Given stock 1 and 10 concurrent checkouts, exactly
one returns `201` and nine return `409 INSUFFICIENT_INVENTORY`. **I1**
**AC-K6** On success `201` with the full order: line snapshots, `gross_minor`, `discount_minor`,
`net_minor`, coupon code if used, customer id, and a sequence number.
**AC-K7** An order is immutable. Changing a product afterwards never changes a past order. **I10**
**AC-K8** Payment is a fake gateway that always succeeds in normal runs; a failing implementation is
injectable in tests to exercise the release paths. [D25]

### 2.4 Idempotency

**AC-I1** Replaying a completed `(cart_id, key)` returns `200` with the identical order body and
header `Idempotent-Replay: true`. The original returned `201`. [D23]
**AC-I2** The key is scoped to `(cart_id, key)`. The same key against a different cart is a
different operation. [D19]
**AC-I3** The same key with a **different body** → `409 IDEMPOTENCY_KEY_REUSED`. The body is
fingerprinted at first use. [D20]
**AC-I4** A replay that arrives while the original is **still in flight** → `409
REQUEST_IN_PROGRESS`. It does not run a second checkout. **I3** [D21]
**AC-I5** Given 10 concurrent requests with one key against stock 1: exactly one `201`, the rest
`409 REQUEST_IN_PROGRESS`, exactly one order exists, inventory decremented exactly once.
**AC-I6** A **failed** checkout releases the key. A subsequent retry with the same key is a genuine
new attempt, not a cached failure. [D22]

### 2.5 Coupons

**The reward rule.** The system is configured with integers `n` and `x`. Defaults `n = 5`, `x = 10`.

> Every `n`-th **successfully placed** order makes **one** coupon for `x`% off available.

Stated exactly:

- Let `P` = the count of orders in state `PLACED`. Only `PLACED` counts. **I8** [D15]
- Milestone `k` (k = 1, 2, 3, …) is **reached** when `P >= k * n`.
- Milestone `k` is **rewarded** by generating exactly one coupon. Each milestone is rewarded at most
  once, ever. **I7**
- The coupon is bound to the customer who placed order number `k * n`. [D12]
- The coupon grants `x`% off the order gross, rounded once, half-even, clamped to gross. [D2] [D3] [D4]
- An order that **redeems** a coupon still counts toward the next milestone. [D16]

**AC-D1** `POST /admin/coupons` generates **one** coupon per call, for the lowest unrewarded reached
milestone. Twelve orders with `n=5` and zero coupons generated requires **three** calls: the first
rewards milestone 1, the second rewards milestone 2, the third returns `409
NO_ELIGIBLE_MILESTONE`. [D14]
**AC-D2** The response includes `pending_milestones` so the caller knows whether to call again.
**AC-D3** Coupons never expire. States: `AVAILABLE` → `RESERVED` → `REDEEMED`, with `RESERVED` →
`AVAILABLE` on failure. [D18] [D30]
**AC-D4** A coupon is redeemable exactly once. **I4**
**AC-D5** A coupon presented by a customer it is not bound to → `422 COUPON_INVALID`, byte-identical
to the response for a code that does not exist. **I6** [D17]
**AC-D6** A coupon already redeemed **by the requesting customer** → `422 COUPON_ALREADY_REDEEMED`.
That is the customer's own coupon; saying so leaks nothing.
**AC-D7** Two concurrent checkouts presenting the same coupon: exactly one redeems it, the other
gets `422 COUPON_ALREADY_REDEEMED` — it reaches the ledger only after the winner has committed.
`COUPON_IN_USE` is unreachable in-process; see [D39]. **I4**
**AC-D8** A checkout that fails *after* reserving a coupon returns it to `AVAILABLE`. The coupon is
never lost. **I5** [D30]

### 2.6 Orders

**AC-O1** `GET /orders/{id}` returns the order. `404 ORDER_NOT_FOUND` otherwise.
**AC-O2** Every order carries a monotonic `sequence` starting at 1, used for milestone attribution.

### 2.7 Report

**AC-R1** `GET /admin/report` returns:

```
orders_placed
items_purchased[]                         product_id, name, quantity
gross_minor / discount_minor / net_minor  sums over PLACED orders
coupons_generated / coupons_available / coupons_reserved / coupons_redeemed
n / x                                     the configured reward values
```

**AC-R2** It is a pure read. Two identical calls return identical bodies. **I11**
**AC-R3** It reconciles: `gross - discount == net` (**I12**) and
`generated == available + reserved + redeemed` (**I15**). `reserved` is exposed precisely so the
identity holds while a checkout is mid-flight. [D31]
**AC-R4** `items_purchased` counts only `PLACED` orders and reconciles with `GET /orders/{id}`.

---

## 3. Non-goals — deliberately excluded

| Excluded | Why |
|---|---|
| Authentication / authorization | Spec says not required. `SAD.md` §1 describes the boundary instead. |
| Real payments | Spec says a fake is acceptable. A gateway interface exists only as a test seam. [D25] |
| Persistence / durability | Spec permits in-memory. `TAD.md` §9 maps every construct to its DB equivalent. [D33] |
| Multi-currency, FX, tax | Not in the spec. Single implied currency, integer minor units. |
| Cart expiry / TTL | Only needed if carts reserved stock. They do not. [D7] |
| Coupon expiry | [D18] |
| Product CRUD, deletion, deactivation | Spec has no such operation. Products are seeded and immutable. |
| Partial fulfilment, backorders, refunds, cancellation | Not in the spec. Orders are terminal. |
| Rate limiting, metrics, tracing, structured logging | Production concerns that do not fit the timebox. |
| Pagination | At seed scale it is noise. |

---

## 4. Ambiguities resolved

Every one of these was a genuine gap in the spec. Full reasoning in `DECISIONS.md`.

| # | Ambiguity | Resolution |
|---|---|---|
| [D5] | Price changes between add-to-cart and checkout | Snapshot at add time; `409 PRICE_CHANGED` at checkout |
| [D6] | How a client escapes `PRICE_CHANGED` | Explicit `POST /carts/{id}/reprice` |
| [D7] | Does add-to-cart reserve stock | No — advisory check only; reservation at checkout |
| [D10] | Customer identity (spec has none) | Five seeded ids, unvalidated |
| [D11] | When customer identity is required | At checkout, in the body — not at cart creation |
| [D12] | Coupon ownership | Bound to the customer who placed the milestone order |
| [D13] | Checkout of an empty cart | `422 CART_EMPTY` |
| [D14] | Backfill: 12 orders, n=5, 0 coupons | One call → one coupon; three calls to drain |
| [D15] | Does a failed checkout count toward the milestone | No — `PLACED` only |
| [D16] | Does a discounted order count toward the next milestone | Yes |
| [D17] | Error for someone else's coupon | `422 COUPON_INVALID`, opaque |
| [D18] | Coupon expiry | None |
| [D2] | Rounding per-line or per-order | Per-order, one rounding site |
| [D3] | Rounding mode | Half-even (banker's) |
| [D19]–[D23] | Idempotency scope, conflict, concurrency, caching, replay status | See `TAD.md` §5 |
| [D25] | Payment abstraction | Yes — a fake gateway, as a test seam |

---

## 5. Out of scope for this document

Module boundaries, signatures, locking, and the error catalogue live in `TAD.md`.
Abuse cases and validation tables live in `SAD.md`. The demo harness lives in `FSD.md`.
