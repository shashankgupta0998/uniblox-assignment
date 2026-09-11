# DECISIONS.md

Design decisions, resolved ambiguities, and deliberate omissions for the checkout and rewards
service.

> **Status: post-build.** §1–§8, §10 and §12 were written before any code and were not changed during
> the build. §9, §11 and §13 were completed after it. [D39] records the one place the build corrected
> the plan.

**How to read this.** Every decision has a stable tag `[D#]` used across `CLAUDE.md`, `PRD.md`,
`TAD.md`, `SAD.md`, `FSD.md`, and `FTL.md`. Tags are never reused or renumbered. §4 gives the full
Context / Options / Choice / Why / Consequences treatment to the eighteen material decisions; §5 lists
the sixteen supporting decisions in compact form, because padding those into long form would obscure which
ones actually mattered.

---

## 1. System invariants

These are the properties the system guarantees. Each is enforced in a named place and protected by a
named test.

| # | Invariant | Enforced in | Test |
|---|---|---|---|
| **I1** | `stock_total == available + reserved + sold` for every product, always, all ≥ 0. Never oversell. | `store.*_locked` under the product lock | C3 |
| **I2** | A cart is checked out at most once | `CartState.CHECKED_OUT` under the cart lock | C4, C10 |
| **I3** | One `(cart_id, key)` yields at most one order under any interleaving | `IdempotencyRegistry.claim` (synchronous) | C4 |
| **I4** | A coupon is redeemed at most once, by at most one order | `reserve_locked` under the ledger lock | C5 |
| **I5** | A coupon is never consumed by a checkout that produces no order | `release_locked` in the `except` path | C6 |
| **I6** | A coupon is redeemable only by its bound customer | `reserve_locked` owner check | C5 |
| **I7** | At most one coupon per milestone, ever | `last_rewarded_milestone` under the ledger lock | C5 |
| **I8** | Only `PLACED` orders increment the order counter | `placed_order_count()` | C8 |
| **I9** | `0 <= discount <= gross`; no negative total | `money.discount_minor` clamp | C2 |
| **I10** | Orders are immutable and self-describing; later product changes never alter them | `OrderLine` price/name snapshots | C7 |
| **I11** | The report is a pure read | `ReportService.build` takes no locks, mutates nothing | C8 |
| **I12** | `gross - discount == net` exactly, per order and in aggregate | `money.net_minor` assertion | C2, C8 |
| **I13** | Inventory reserved by a failed checkout is released in full | `release_inventory_locked` in the `except` path | C6 |
| **I14** | All money is integer minor units; no float in any money path | `int` throughout; `Decimal` only inside `money.py` | C2 |
| **I15** | `generated == available + reserved + redeemed`, always | coupon state machine under the ledger lock | C8 |

**On I14 specifically:** there is no client-supplied money value anywhere in the API. Prices come only
from seed data and `x` only from config, so the only client influence on any total is a quantity
bounded to `[1, 100]`. That is the strongest single property in the design and it makes most
money-corruption attacks structurally impossible rather than merely validated against.

---

## 2. Ambiguities found, and the semantics selected

The spec is explicit that some coupon and cart semantics are undefined and must be chosen. These are
the gaps found, and what was chosen. Reasoning for the material ones is in §4.

| Ambiguity | Chosen semantics | Tag |
|---|---|---|
| Price changes between add-to-cart and checkout | Snapshot at add time; checkout fails `409 PRICE_CHANGED` rather than silently charging either price | [D5] |
| How a client escapes `PRICE_CHANGED` | Explicit `POST /carts/{id}/reprice` — accepting a price change is an action, not a side effect | [D6] |
| Does add-to-cart reserve inventory | No. Advisory check only; reservation happens at checkout | [D7] |
| Customer identity (the spec has none) | Five seeded ids, supplied at checkout, never verified | [D10] [D11] |
| Coupon ownership | Bound to the customer who placed the milestone-hitting order | [D12] |
| Coupon backfill: 12 orders, n=5, 0 coupons generated | One admin call generates **one** coupon. Three calls to drain: milestone 1, milestone 2, then `409` | [D14] |
| Does a failed checkout count toward the milestone | No. `PLACED` only — the spec says "successfully placed" | [D15] |
| Does a discounted order count toward the next milestone | Yes. Milestones do not reset on redemption | [D16] |
| Coupon expiry | None. Coupons are valid until redeemed | [D18] |
| Checkout of an empty cart | `422 CART_EMPTY`. No zero-line order is ever created | [D13] |
| Discount rounded per-line or per-order | Per-order. Exactly one rounding site in the system | [D2] |
| Rounding mode | Half-even (banker's) | [D3] |
| Error when presenting another customer's coupon | `422 COUPON_INVALID`, byte-identical to an unknown code | [D17] |
| Payment abstraction, or checkout-success-is-payment-success | A fake gateway behind an interface — kept because it is the test seam, not for realism | [D25] |
| Idempotency key scope | `(cart_id, key)` | [D19] |
| Same key, different payload | `409 IDEMPOTENCY_KEY_REUSED` via body fingerprint | [D20] |
| Same key arriving while the original is in flight | `409 REQUEST_IN_PROGRESS` | [D21] |
| Whether failures are cached under the key | No. Successes only | [D22] |
| Status code for a successful replay | `200` + `Idempotent-Replay: true` (original was `201`) | [D23] |

---

## 3. What was NOT ambiguous

Three things that look like open questions but are not, recorded so the reasoning is visible:

- **"Successfully placed order"** appears twice in the spec. It settles [D15] — a failed checkout
  cannot count. This was treated as reading the spec, not as a design choice.
- **Product deletion / deactivation** has no endpoint in the spec, so no cart-holds-a-dead-product
  case exists. Not handled, deliberately.
- **Order cancellation and refunds** are absent from the spec. `PLACED` is the only order state,
  which is why the inventory state machine has no return path from `sold`.

---

## 4. Material decisions

---

### Decision: Python + FastAPI rather than the stated house stack `[D27]`

**Context:** The FAQ notes the team primarily works with TypeScript/Node.js, and adds "use what lets
you demonstrate your skills best." The assignment is graded on concurrency, idempotency and invariant
reasoning within 4–6 hours. Stack choice is not itself graded.

**Options considered:**
- *TypeScript + Node + Express.* Matches the house stack and the reviewer's likely fluency. I have
  shipped Next.js/TypeScript, but have never written a production Node backend service — no Express,
  no Node HTTP server under load.
- *Go.* Genuine thread parallelism and `go test -race` as machine-generated evidence. I have no Go
  experience.
- *Python + FastAPI.* My strongest backend surface — five years, production FastAPI, including the
  accounting and rate engine of a multi-tenant booking platform.

**Choice:** Python + FastAPI, with `asyncio.Lock` for the critical sections.

**Why:** asyncio has the same concurrency semantics as Node — one event loop, no preemption inside a
synchronous block, interleaving only at `await` — so the reasoning being graded is identical in
either. What differs is the non-graded overhead. Node has no stdlib async mutex, which would have
meant hand-rolling the single most load-bearing component here on a first attempt; no half-even
rounding, so [D3] would also have been hand-rolled; and Vitest would have been a new testing surface.
Python supplies `asyncio.Lock`, `decimal.ROUND_HALF_EVEN` and pytest from the standard toolchain, and
FastAPI generates the OpenAPI document the spec lists as an accepted documentation deliverable at
zero cost. The estimated difference was 60–105 minutes of setup and learning — 20–35% of the budget,
taken directly from the graded portion.

I also weighed the reverse risk, which turned out to be the deciding argument: **the house-stack
advantage inverts when you are not fluent in it.** A TypeScript reviewer reading clean, annotated
Python judges the reasoning, because the mental model transfers. The same reviewer reading first-time
Express judges the Express — they would spot every non-idiom, because it is their home turf. Choosing
Node would have incurred the unfamiliarity penalty *and* forfeited the fluency benefit.

The specific Node hazards priced in: Express 4 does not catch rejected promises from `async`
handlers, so an async handler that throws hangs the request with no response — a failure that
presents under concurrent load as "his concurrency control deadlocks."

**Consequences:** The submission is off the house stack, which is a real cost if reviewer fluency
matters more than I have weighted it. Mitigated with full type annotations and deliberately unclever
Python — no metaclasses, no decorator tricks. I also give up `go test -race`: there is **no automated
race detector** in this submission. The evidence for the concurrency invariants is deterministic
tests that fail when the lock is removed (C3), not tooling.

---

### Decision: Money as integer minor units, rounded once per order, half-even `[D1] [D2] [D3] [D4]`

**Context:** The spec requires calculating money without floating-point error. A percentage discount
forces a rounding decision: where it happens, how many times, and which direction.

**Options considered:**
- *Float / `Decimal` throughout.* `Decimal` is exact but invites division and non-integer
  intermediates; float is disqualified outright.
- *Per-line rounding.* Round each line's discount, then sum.
- *Per-order rounding.* Sum lines to a gross, round once.
- *Rounding mode:* half-up (retail convention), half-even (no systematic bias), floor (always favours
  the house).

**Choice:** Money is Python `int` in minor units (paise), every field suffixed `_minor`. Discount is
computed **once per order** — `Decimal(gross) * percent / 100`, quantized to 1 with
`ROUND_HALF_EVEN`, converted straight back to `int`, then clamped `min(discount, gross)`. `Decimal`
appears in exactly one module and never escapes it.

**Why:** Integer minor units make "no floating-point error" a property of the type rather than a
discipline — there is no representation to get wrong. Per-order rounding gives **exactly one rounding
site in the entire system**, which makes the money story auditable in one function and makes report
reconciliation exact: the sum of order discounts is the reported total discount, with no
rounded-sum-versus-sum-of-rounded discrepancy. Per-line rounding would only be necessary if lines had
differing discount eligibility, which they do not. Half-even avoids the systematic upward bias
half-up introduces across many orders; the store rounds a half-paise neither consistently toward the
customer nor consistently toward itself. The clamp is what makes **I9** unconditional rather than a
consequence of `x <= 100` — it holds even if `x` is misconfigured.

**Consequences:** Every response exposes `_minor` integers, so any client must divide for display —
accepted, and the demo harness does exactly that, for display only. Half-even will occasionally
surprise someone who expects half-up; the tests assert a case where the two disagree so the behaviour
is pinned, not incidental. A future multi-currency requirement would need a per-currency exponent;
that is a non-goal here.

---

### Decision: Cart snapshots price at add time; checkout fails on drift `[D5] [D6]`

**Context:** The spec explicitly requires deciding what happens when price or availability changes
between add-to-cart and checkout.

**Options considered:**
- *Resolve price fresh at checkout.* No drift is possible; the customer simply pays the current
  price. Simplest, fewest states — but it makes the spec's question have the answer "nothing
  happens."
- *Snapshot at add time and honour the snapshot.* A price lock. Turns the cart into a free option on
  the price, and makes indefinite cart-holding an abuse vector.
- *Snapshot at add time, detect drift at checkout, fail.*

**Choice:** Snapshot `unit_price_minor` on add and update; at checkout compare every snapshot to the
live price and fail with `409 PRICE_CHANGED`, whose body enumerates `{product_id,
old_unit_price_minor, new_unit_price_minor}` for each changed line. Recovery is an explicit
`POST /carts/{id}/reprice` that re-snapshots and returns the new totals.

**Why:** Charging a price the customer never saw is the failure mode worth preventing, and honouring
a stale price indefinitely is a giveaway. Failing puts the decision back where it belongs — with the
customer — and the enumerated diff is what makes the error "useful to an API client" as the spec
requires, rather than forcing a re-fetch to discover what moved.

The reprice endpoint exists to close a dead end: without it, a drifted cart returns `409` on every
retry forever and is permanently stuck. The considered alternative was re-`PUT`ting a line's existing
quantity to force a re-snapshot as a side effect — zero new API surface, but a genuinely unpleasant
thing to document and a per-line loop for the client. A third option, `expectedTotal` on the checkout
request as optimistic concurrency on price, was rejected: because the snapshot already lives in the
cart, `GET /carts/{id}` returns stale totals until something re-snapshots, so `expectedTotal` would
not have removed a round trip — it would only have moved recovery into a frozen request signature.

**Consequences:** One extra endpoint and one extra error path. Carts can go stale and require an
explicit customer action. Notably, drift detection is *cheap* — it is a synchronous equality check
with no race, unlike inventory (see [D7]).

---

### Decision: Add-to-cart is advisory; inventory is reserved only at checkout `[D7] [D9]`

**Context:** The spec requires that invalid quantities not silently enter a cart, and that the system
never oversell. Those are different requirements, and conflating them determines where the concurrency
lives.

**Options considered:**
- *Reserve stock at add-to-cart.* Carts become holds. Requires cart expiry to be sane, or abandoned
  carts sleep on inventory permanently — and expiry is a non-goal.
- *Snapshot availability at add time and raise a distinct drift error at checkout.* An extra field and
  an extra error code carrying no information the shortfall error does not already carry.
- *Check at add time without reserving; reserve under lock at checkout.*

**Choice:** The add-time check is advisory: a quantity above current stock is rejected with
`422 QUANTITY_EXCEEDS_STOCK`, but **nothing is held**. Ten carts may each contain the last unit.
Reservation happens inside the checkout critical section, under the product lock, and a shortfall
there returns `409 INSUFFICIENT_INVENTORY`.

**Why:** Price drift is a *stale-snapshot* problem — a deterministic equality check, same answer every
time. Inventory is a *live-contention* problem — the answer changes between two reads, and two
concurrent checkouts can both observe sufficient stock unless check-and-decrement is atomic. Keeping
them as separate concepts with separate codes and separate statuses is what makes the API honest:
`422` means "your request was wrong", `409` means "you lost a race". Reserving at add time would move
the interesting concurrency to the wrong endpoint and would force cart TTL into scope.

Two distinct codes rather than one code with two statuses [D9] because a stable code must map to
exactly one HTTP status, or clients cannot switch on it reliably.

**Consequences:** A customer can hold an item in a cart and still lose it at checkout — correct
behaviour, and the reason `INSUFFICIENT_INVENTORY` is `409` rather than `422`. The add-time check is a
usability affordance, **not a guarantee**, and is never claimed as one. Because carts hold nothing,
no cart TTL, sweeper, or reservation reaper is needed anywhere in the system.

---

### Decision: `asyncio.Lock` with a total order over lock classes `[D24]`

**Context:** In-memory state shared across concurrent requests, with a mandate to demonstrate that
invariants hold when requests overlap.

**Options considered:**
- *No locks.* Write every critical section synchronously. On a single event loop this is genuinely
  atomic and genuinely correct — but it collapses the moment anything inside needs I/O, and it leaves
  the spec's central evaluation criterion unaddressed.
- *One global lock.* Trivially correct, but serialises every checkout, so it cannot demonstrate that
  independent operations proceed concurrently.
- *Per-resource locks with a documented acquisition order.*

**Choice:** Three lock classes — per-product, one global coupon ledger, per-cart — acquired in the
single permitted order **products (ascending id) → coupon ledger → cart**. `LockManager.acquire` is
the only way to obtain a lock, so the order is enforced by construction rather than by discipline.

**Why:** Per-product locks mean two checkouts for different products genuinely run concurrently;
one global lock would prove nothing. A **total order over lock classes makes deadlock structurally
impossible**: a cycle requires two holders each waiting on what the other holds, which requires at
least one to acquire out of order. Sorting product ids handles the intra-class case — two checkouts
touching `{dock, headset}` in opposite cart-insertion order would otherwise deadlock. `asyncio.Lock`
is not reentrant, which is why the coupon reserve/commit/release methods are synchronous and
non-locking: a self-locking variant called from inside the checkout's ledger lock would deadlock
immediately.

This was verified rather than assumed. Measured before the design was fixed: 10 coroutines × 10,000
synchronous decrements lose **zero** updates; 100 coroutines with an `await` inside an unlocked
critical section **oversell 99 of 100**; the same with `asyncio.Lock` spanning the await lose zero.

**Consequences:** The ordering rule is a standing constraint on all future code — any new operation
must fit the hierarchy or extend it deliberately. `LockManager` centralises that, so violating it
requires bypassing the abstraction rather than merely forgetting a convention. Under multiple
instances the rule survives but the mechanism does not; see §10.

---

### Decision: Every mutation is a synchronous function; exactly one `await` in the critical section `[D24] [D25]`

**Context:** On a single event loop, correctness reduces to a question about yield points. Making
those points auditable rather than incidental is worth an architectural rule.

**Options considered:**
- *Async methods throughout*, letting `await` appear wherever convenient.
- *All state mutation synchronous*, with `await` confined to lock acquisition and genuine I/O.

**Choice:** All state mutation is written as synchronous functions. Methods requiring a held lock are
suffixed `_locked` and are `def`, not `async def`. The checkout critical section contains **exactly
one `await`**: the payment gateway call.

**Why:** A synchronous block cannot yield, so it cannot interleave — the type signature carries the
atomicity guarantee, and `grep -n await src/core/checkout.py` audits it in one command. Two
consequences fall out for free. First, `IdempotencyRegistry.claim` needs no lock at all: being
synchronous, its check-and-insert is already atomic. Second, `ReportService` needs no locks, because
the commit block is one synchronous run and no reader can observe inventory decremented without the
order existing.

**Consequences:** A future contributor adding an `await` inside a critical section silently breaks
atomicity, which is why the rule is stated in `CLAUDE.md` as a hard constraint with a grep to check
it. The lock-free report is correct **only** in a single process; under multiple instances it needs a
`REPEATABLE READ` transaction (§10).

---

### Decision: A payment abstraction, kept for the test seam rather than for realism `[D25]`

**Context:** The spec permits treating checkout success as payment success, or introducing a small
abstraction, and asks for the choice to be explained.

**Options considered:**
- *No abstraction.* Checkout success is payment success. Zero extra code.
- *A `PaymentGateway` protocol with a fake implementation.*

**Choice:** A `PaymentGateway` protocol, a `FakePaymentGateway` that always succeeds, and an
`AlwaysDeclineGateway` injected in tests. `FakePaymentGateway.charge` awaits `asyncio.sleep` even at
zero latency.

**Why:** Two reasons, and neither is realism. First, it is the cheapest seam for forcing a failure
*after* inventory and a coupon have been reserved, which is the only way to test **I5** ("a coupon
must not be consumed by a checkout that ultimately fails") and **I13** without contriving one.
Second — and this is the one that mattered — **the awaited payment call is what makes the mutex
load-bearing.** Without an `await` inside the critical section, the section is atomic by construction
on a single event loop, the lock protects nothing that could interleave, and the entire concurrency
design becomes untestable ceremony. The gateway is not there to model payments; it is there to create
the yield point the design exists to guard.

**Consequences:** `FakePaymentGateway` must await even at zero latency, or the property it exists to
provide silently disappears — stated explicitly in ticket A5's definition of done. The payment call
is made **while holding the product and ledger locks**, which is a real availability weakness: a slow
gateway blocks every other checkout for those products. See §9 and `SAD.md` A8.

---

### Decision: Idempotency keys scoped to `(cart_id, key)` `[D19]`

**Context:** The spec requires that a retried checkout not create a second order. The key's
uniqueness domain determines what happens when two clients choose the same key.

**Options considered:**
- *Global.* One map for the whole system.
- *`(customer_id, key)`.* The industry-standard scope — Stripe namespaces keys per account.
- *`(cart_id, key)`.*

**Choice:** `(cart_id, key)`.

**Why:** The risk is not UUID collision, which is negligible; it is that real clients are lazy. A
client that sends `Idempotency-Key: 1` on every checkout is **safe** under cart-scoping, because each
checkout targets a different cart and each cart carries its own namespace. Under `(customer_id, key)`
that same client's second order collides with its first and silently receives the **wrong order**.
Under global scope it receives **another customer's order** — a cross-customer data leak caused by
nothing but a naming convention.

The operation being made idempotent is "check out cart X", so scoping the key to the resource it acts
on is also the more honest model. It layers with an independent defence: even a client that retries
with *no key at all*, or a fresh key, cannot double-order, because the cart is already `CHECKED_OUT`
(**I2**). The two mechanisms protect different things — idempotency guarantees the retry receives the
*same response*; cart single-use makes a duplicate order *structurally impossible*.

**Consequences:** Departs from the conventional per-account scope, which is worth being able to
explain: Stripe's operations are not tied to a single-use resource, and ours are. A key reused across
*different* carts produces two orders — but that is a genuinely different request, not a retry.

---

### Decision: The idempotency key is claimed before the work, and a concurrent replay is refused `[D21]`

**Context:** The spec's motivating scenario is a client that timed out and retried — which means the
retry can arrive **while the original is still running**. This is the case naive implementations get
wrong.

**Options considered:**
- *Look up, run, store.* The obvious implementation.
- *Claim first, then join the in-flight request* and return its result to the retry.
- *Claim first, then refuse* the concurrent retry.

**Choice:** `claim()` performs a synchronous insert-if-absent **before** any lock is acquired, writing
an `IN_PROGRESS` record. A retry that finds `IN_PROGRESS` gets `409 REQUEST_IN_PROGRESS`.

**Why:** Look-up-then-run is broken, and it is broken exactly in the scenario the spec cares about:

```
t=0  R1 (key K) arrives.  Lookup: ABSENT.  Proceeds.
t=1  R1 reserves inventory and the coupon.
t=2  R1 awaits payment.                       <-- YIELDS
t=3  R2 (key K) arrives.  Lookup: ABSENT — R1 has stored nothing yet.  Proceeds.
t=4  R2 reserves inventory AGAIN.
     => two orders, inventory charged twice
```

The map only helps *after* the first request finishes, so the key must be claimed before the work
begins. Because `claim` contains no `await`, its check-and-insert cannot interleave — the event loop
is the mutex for that one function.

Refusing rather than joining, for three reasons: joining pins every retry in server memory awaiting a
single slow gateway, converting retries into memory pressure exactly when the system is already
struggling; joining forces a definition of what the joiner receives when the original *rejects*, a
subtle branch with real bug potential in a tight budget; and refusing composes cleanly with replay —
`409 REQUEST_IN_PROGRESS` → client backs off → retries → the original has completed → `200` +
`Idempotent-Replay: true`. Two responses, one unambiguous meaning each.

**Consequences:** Clients must implement retry-with-backoff; a client that does not will see an error
for a request that is about to succeed. This is a worse client experience than joining, and it is the
deliberate trade for a mechanism that is simple enough to be obviously correct and deterministic
enough to test (C4 asserts exactly one `201` and nine `409`s).

---

### Decision: Same key with a different payload is rejected, not replayed `[D20]`

**Context:** A key can arrive twice with different bodies — a client bug, or a captured request
replayed with modifications.

**Options considered:**
- *Ignore the body, replay the original response.* Common, and roughly what several real APIs do.
- *Fingerprint the body at first claim; reject a mismatch.*

**Choice:** SHA-256 of the canonicalised `{cart_id, customer_id, coupon_code}` is stored at first
claim. A mismatch returns `409 IDEMPOTENCY_KEY_REUSED`, checked **before** the in-progress check.

**Why:** The coupon case makes replay-and-ignore indefensible. First request carries `SAVE10`, the
retry omits it: replay-and-ignore returns a *discounted* order to a caller that asked for no discount.
Reverse it and the server silently drops a coupon the caller believes was applied. Both are silent
wrong-money outcomes, and both are invisible to the client. Same key must mean same request; if the
body differs, either the client has a bug or someone is replaying a captured key, and failing loudly
is right in both cases. The cost is roughly ten lines using `hashlib` and `json.dumps(sort_keys=True)`.

The fingerprint is computed **inside** `CheckoutService`, not in the router, so the API layer cannot
drift from the domain layer about what constitutes "the same request".

**Consequences:** Fingerprinting proves payloads match; it proves nothing about *who* sent them.
Without authentication, a byte-identical replay from an attacker receives a byte-identical response —
which is correct idempotent behaviour and is precisely why this is not a defence against
impersonation (`SAD.md` A3).

---

### Decision: Only successes are recorded under the key `[D22]`

**Context:** When a checkout fails, the `IN_PROGRESS` record must either become a cached failure or
be released.

**Options considered:**
- *Cache every outcome uniformly.* One rule, simple to state.
- *Cache terminal client errors, release transient ones.* The sophisticated-looking option.
- *Cache successes only; release on any failure.*

**Choice:** Successes only. Any failure releases the record.

**Why:** Caching failures poisons the key. A client that receives `409 PRICE_CHANGED`, calls
`reprice`, and retries with the same key would receive the **cached** `PRICE_CHANGED` forever, and
would have to mint a new key to recover from a problem the server just told it to fix. The
"terminal vs transient" taxonomy does not rescue this: `PRICE_CHANGED` would have to be carved out,
then `COUPON_INVALID` (an admin may generate a coupon), then `INSUFFICIENT_INVENTORY` (stock may
return) — until nearly everything is transient and the classification was busywork. Re-running
validation on a retry costs nothing, since nothing was committed.

**Consequences:** This is a deliberate departure from strict replay semantics — the same key can
legitimately yield a different result over time. The guarantee offered is precisely "**at most one
order**", not "a frozen response for all time", and that is what the documentation says. Stated
plainly because a reviewer may reasonably challenge it.

---

### Decision: Coupons are bound to the customer who triggered the milestone `[D10] [D11] [D12]`

**Context:** The spec defines no customer entity, yet requires deciding who may redeem a generated
coupon.

**Options considered:**
- *Global pool, first-come-first-served.* Requires inventing nothing — defensible purely on "the spec
  has no customer".
- *Bound to a customer.* Requires inventing an identity model and defending it.

**Choice:** Five seeded customers (`cus_1`…`cus_5`). A coupon is bound to the customer who placed
order number `k × n`. `customer_id` is **not** required at cart creation and **is** required at
checkout, supplied in the request body.

**Why:** A reward for purchasing activity that any stranger can spend is not a reward — binding is
what makes the coupon a loyalty mechanism rather than a race. A seeded list rather than
client-invented ids keeps `422 UNKNOWN_CUSTOMER` meaningful and gives the report a real dimension.

Requiring identity only at checkout follows from what the cart *is*: an anonymous, disposable
container. Nothing before checkout needs to know who is shopping, and demanding identity earlier would
be ceremony. It sits in the **body rather than a header** so it is covered by the [D20] fingerprint
automatically — a retry with the same key but a different customer is caught as a payload mismatch
rather than needing a special case.

**Consequences:** `customer_id` is an unverified claim; anyone may assert any of the five ids and
redeem that customer's coupon. This is open by construction because authentication is out of scope,
and it is documented rather than papered over (`SAD.md` §1, A4). It is also why the wrong-owner error
is deliberately opaque [D17]: with only five possible owners, confirming that a code exists would
reduce theft to five attempts.

---

### Decision: One admin call generates one coupon `[D14]`

**Context:** With `n = 5` and twelve placed orders and no coupons yet generated, two milestones are
outstanding. Does one admin call produce one coupon or two?

**Options considered:**
- *One call drains all outstanding milestones,* returning an array.
- *One call, one coupon,* with the caller repeating until refused.
- *One call, one coupon, plus a `pending_milestones` count* in the response.

**Choice:** One call generates exactly one coupon, for the lowest unrewarded reached milestone, and
the response includes `pending_milestones`. Twelve orders therefore need three calls: milestone 1,
milestone 2, then `409 NO_ELIGIBLE_MILESTONE`.

**Why:** It reads the spec's rule literally — "a coupon is generated only if the milestone has been
reached and a coupon has not already been generated **for that milestone**" is a per-milestone
statement. It keeps the endpoint's return type singular, which keeps the response schema and the
concurrency story simple: `POST /admin/coupons` is one atomic reward of one milestone, and N
concurrent calls at one milestone produce exactly one coupon (**I7**, test C5). Returning an array
would have made the success shape vary with backlog depth for no benefit. `pending_milestones` is the
cheap fix for the only real objection — the caller would otherwise have to poll blindly.

**Consequences:** Draining a large backlog requires repeated calls. Acceptable: this is an
administrative operation, backlogs are small, and the count tells the caller exactly how many calls
remain.

---

### Decision: Discounted orders still count toward the next milestone `[D16]`

**Context:** Does an order that redeems a coupon count toward the *next* milestone, or is the counter
suppressed for rewarded purchases?

**Options considered:**
- *Excluded.* Coupons cannot compound; each reward requires `n` fresh full-price orders.
- *Counted.* An order is an order.

**Choice:** Counted. The milestone counter is a count of `PLACED` orders and nothing else.

**Why:** It keeps **I8** a single, checkable statement — `placed_order_count()` is the count of orders
in state `PLACED`, with no conditional. Any exclusion rule introduces a second notion of "counting
order" that the report must then reconcile against, and the spec's own phrasing ("every *n*th
successfully placed order") contains no such qualifier.

**Consequences:** This one has a real cost. Combined with no minimum order value and unverified
customer identity, this makes coupon farming cheap and *compounding*: place `n` orders for a ₹399
cable to mint a 10% coupon, spend it on a ₹24,999 dock, and the discounted order itself counts toward
the next milestone, so cycles never reset. Analysed fully in `SAD.md` A1. The cheapest real mitigation
is a minimum order value for milestone eligibility — a single `if` at the counter, deliberately not
implemented because it is a product decision the spec does not make. Reversing this decision is a
one-line change, which makes it a good candidate if asked to change a business rule.

---

### Decision: "Not your coupon" is indistinguishable from "no such coupon" `[D17]`

**Context:** A customer presents a coupon bound to someone else. The spec separately asks for errors
that are "distinguishable and useful to an API client."

**Options considered:**
- *`403 COUPON_NOT_OWNED`.* Precise and debuggable.
- *`422 COUPON_INVALID`,* identical to a code that does not exist.

**Choice:** `422 COUPON_INVALID`, byte-identical in both cases. The real reason is written to the
server log.

**Why:** There is a concrete attack chain otherwise. `COUPON_NOT_OWNED` confirms that a guessed code
is real; owners are a seeded set of **five** and `customer_id` is completely spoofable; so a confirmed
code costs at most five further attempts to redeem. An identical response for both cases removes the
oracle and makes the five-owner search worthless. Debuggability is preserved by logging the precise
reason server-side — opaque to the caller, precise in the logs.

On the tension with the spec's "distinguishable errors": distinguishable **by a cause the caller can
act on**. A caller can do nothing about a coupon belonging to someone else, so the distinction buys
them nothing and costs an enumeration oracle. A coupon that is already redeemed **and owned by the
requesting customer** does return the distinct `COUPON_ALREADY_REDEEMED` — that is their own coupon,
and saying so leaks nothing.

**Consequences:** Slightly harder third-party debugging, mitigated by logs. Coupon codes must be
`uuid4`-derived rather than milestone-derived, or `COUPON5` would be guessable by construction and the
opacity would be pointless.

---

### Decision: The report exposes `reserved` and takes no locks `[D31]`

**Context:** The spec requires the report to reconcile with orders and coupons, and requires repeated
report requests not to mutate state.

**Options considered:**
- *Report only committed state,* omitting in-flight reservations.
- *Acquire all locks* for a consistent snapshot.
- *Lock-free read, with `reserved` exposed as a first-class count.*

**Choice:** `ReportService.build` takes no locks and reports `coupons.reserved` alongside
`available` and `redeemed`.

**Why:** It is safe to read without locks precisely because of [D24]'s synchronous-mutation rule: the
commit block contains no `await`, so no reader can observe inventory decremented without the order
existing. Taking locks would make a pure read block on checkouts for no correctness gain and would
risk violating the ordering rule from a read path.

Exposing `reserved` is what keeps the reconciliation identity **exact at every instant**:
`generated == available + reserved + redeemed` holds even while a checkout is mid-flight. Hiding
reservations would make the identity appear to break during concurrent load — exactly when a reviewer
is most likely to be watching — and "it reconciles except during checkouts" is a much weaker claim
than an unconditional one.

**Consequences:** The report exposes an internal state that a customer-facing report would not.
Acceptable: it is an administrative endpoint, and the transparency is the point. Under multiple
instances the lock-free argument does **not** survive and the read needs `REPEATABLE READ` (§10).

---

### Decision: Demonstrate the design's own limitation with a threadpool test `[D26]`

**Context:** FastAPI dispatches `async def` handlers to the event loop and plain `def` handlers to a
threadpool. `asyncio.Lock` is not thread-safe, so a single mis-declared handler would silently void
every invariant.

**Options considered:**
- *Say nothing;* declare every handler `async def` and move on.
- *Note it in prose only.*
- *Ship a test that demonstrates the failure.*

**Choice:** All production handlers are `async def`. One test drives `CheckoutService` from a
`ThreadPoolExecutor` and **demonstrates the invariant breaking**, documenting the boundary of the
guarantee.

**Why:** The spec says the follow-up discussion may ask the candidate to identify a weakness in their
implementation. A demonstrated answer is stronger than a verbal one, and knowing the limits of one's
own mechanism reads better than claiming it has none. The claim is: *my mutual exclusion is an
`asyncio.Lock`; it is not thread-safe; that is sound only because every state-touching handler is
`async def`; here is the test that shows what happens if one is not.*

The test must be deterministic, not flaky. Verified: a pure-CPU threaded race does **not** reproduce
at Python's default 5 ms switch interval and requires lowering `sys.setswitchinterval` to force —
which would read as contrived. With **blocking I/O inside the critical section**, which is exactly
what a `def` handler calling a payment gateway looks like, it oversells 95 of 100 at stock settings.
The test uses the blocking-I/O form for that reason.

**Consequences:** One extra test file with no production value, marked cuttable in `FTL.md`. Two
execution models are deliberately **not** run in production code — that would double the surface for
no grading benefit.

---

### Decision: The coupon-race loser receives `COUPON_ALREADY_REDEEMED`, not `COUPON_IN_USE` `[D39]`

**Context:** The plan specified that N concurrent checkouts presenting one coupon yield one `201` and
N−1 `409 COUPON_IN_USE` — the losers observing the coupon in `RESERVED`. The critic session's C5 test
could not produce that outcome, and worked out why rather than weakening the assertion.

**Options considered:**
- *Keep as built.* Losers receive `422 COUPON_ALREADY_REDEEMED`; `COUPON_IN_USE` stays in the catalogue,
  annotated as unreachable in-process.
- *Release the ledger lock before the payment `await`* so that `RESERVED` becomes observable and
  `COUPON_IN_USE` is reachable.
- *Delete `COUPON_IN_USE`* from the enum and catalogue.

**Choice:** Keep as built. The orchestrator session ruled this during the build as an explicitly
reversible assumption and flagged it for me; I ratified it after the build with the reasoning below.

**Why:** The coupon-ledger lock is held across the payment `await` by design — that is the entire
mechanism of [D24] and [D25]. Under it, a loser cannot enter `reserve_locked` until the winner has
released the lock, by which point the coupon is `REDEEMED` (or `AVAILABLE`, if the winner's payment
failed — in which case the "loser" simply wins). `RESERVED` is observable only by the lock-free report
[D31], which is exactly why **I15** exposes it. Releasing the lock early would reopen the race the lock
exists to close. Deleting the code would change a frozen interface to remove a state that is
**forward-correct**: under the §10 production design, if payment moves outside the database
transaction, competing checkouts *do* observe `RESERVED`, and `COUPON_IN_USE` becomes reachable.

**Consequences:** PRD AC-D7, TAD §7, SAD A9, FSD §6 and FTL C5 were corrected after the build to match
the code. `COUPON_IN_USE` remains in the enum, raised by `reserve_locked` on a `RESERVED` coupon, and is
reachable in-process only from the threadpool demonstration [D26], where the locks do not serialise.
This is the one place the plan specified a state the design it described cannot produce. The critic
found it by testing, not by reading — which is the gate doing its job.

---

## 5. Supporting decisions

Real decisions that did not warrant long form. Expanding these would obscure which ones actually
mattered.

| Tag | Decision | Why |
|---|---|---|
| `D8` | Cart single-use enforced by a `CHECKED_OUT` state under the cart lock | A second defence layer independent of idempotency: a retry with no key, or a fresh key, still cannot double-order |
| `D9` | `QUANTITY_EXCEEDS_STOCK` (422) and `INSUFFICIENT_INVENTORY` (409) are distinct codes | One code must map to exactly one status, or clients cannot switch on it. Also encodes the client-error / contention distinction from [D7] |
| `D13` | Empty-cart checkout → `422 CART_EMPTY` | A zero-line order pollutes the report's order count and would let anyone farm milestones for free at zero cost |
| `D15` | Only `PLACED` orders count toward milestones | The spec says "successfully placed" twice; treated as reading the spec, not choosing |
| `D18` | Coupons never expire | A TTL costs a clock abstraction, a time-travel test helper, and a fourth coupon state. A schema field that is never enforced would be worse than either |
| `D23` | Successful replay returns `200` + `Idempotent-Replay: true`; the original returned `201` | `201` asserts a resource was created, which is false on replay. The header makes replay observable in the demo harness |
| `D28` | Admin marked by `/admin` prefix **and** an `X-Admin-Token` header | The prefix alone is a naming convention no code checks. A token check is a real enforcement point a reviewer can put a finger on, even with a constant secret |
| `D29` | Uniform error envelope; `DomainError` carries its own code and status | The API layer maps errors generically and never switches on the code, so adding an error cannot desynchronise the two layers |
| `D30` | Coupon lifecycle `AVAILABLE → RESERVED → REDEEMED` with release-on-failure | A two-state model cannot express "held by an in-flight checkout", which is what makes **I5** and the concurrent-redemption case (**I4**) expressible at all |
| `D32` | Quantity bounded `[1, 100]`, `strict=True` | Python ints are arbitrary precision, so unlike JavaScript there is no silent wraparound — the failure mode is resource exhaustion, so an **upper** bound is required, not just a lower one (`SAD.md` A5) |
| `D33` | `InMemoryStore` with no repository abstraction | A one-implementation interface is speculative generality at this size. The store *is* the seam; §10 says what replacing it costs |
| `D34` | Frontend is a demo harness, explicitly cuttable | The spec says frontend cannot compensate for an unreliable backend. Its only job is making concurrency and idempotency visible |
| `D35` | `n`, `x`, admin token, payment latency from env with defaults | `n` and `x` must be configurable because the spec says "configure the system with values n and x". Payment latency is configurable because the concurrency tests need a real in-flight window |
| `D36` | Pydantic `strict=True` and `extra="forbid"` on every request model | Silently ignoring unknown fields is how a `coupon_code` typo becomes an undiscounted order the customer expected to be discounted |
| `D37` | All external ids are `uuid4`-derived and opaque | `GET /carts/{id}` and `GET /orders/{id}` have no ownership check, so id entropy is the only confidentiality defence (`SAD.md` A4). `Order.sequence` is internal and not addressable |
| `D38` | Built by three sessions — an orchestrator that writes no code, a worker owning `src/`, and a critic owning `tests/` that gates every ticket | The worker cannot satisfy the gate by editing tests, because it cannot edit them; the critic cannot patch its way to PASS, because it cannot edit `src/`. `CLAUDE.md` + frozen `TAD.md` §3 are the entire contract; stub-first (`A1`) is what lets the critic write red tests while the worker builds |

---

## 6. Transaction, concurrency, and idempotency strategy

Full detail in `TAD.md` §4–§5. The summary:

**There are no transactions.** With in-memory state there is nothing to roll back, so atomicity is
achieved by two mechanisms instead: (1) mutual exclusion via ordered `asyncio.Lock`s, and (2) the rule
that all mutation is synchronous, so a mutating block cannot be interleaved. Compensation is manual —
the `except` path in `CheckoutService` explicitly releases inventory, the coupon, and the idempotency
record.

**The locks**: one per product (guards `available`/`reserved`/`sold` — **I1**), one global coupon ledger (guards coupon states, `last_rewarded_milestone`, and the order log — **I4 I5 I7**), one per cart (guards items and `CartState` — **I2**). Acquired only through `LockManager.acquire`, which enforces the order **products (ascending id) → coupon ledger → cart** by construction; a total order over lock classes makes deadlock structurally impossible.

**The critical section**, in order: claim the idempotency key (synchronous, before any lock) → acquire
products (sorted) → coupon ledger → cart → verify → reserve inventory → reserve coupon → compute
totals → **`await` payment** (the only await) → commit synchronously → complete the key. Any exception
releases everything and re-raises.

**Why the lock is load-bearing and not ceremony:** the payment call is awaited *inside* the critical
section, after inventory and coupon reservation. That await is a genuine yield point at which another
checkout observes half-applied state. Measured: 99 of 100 oversold without the lock, 0 with it.

**Idempotency in one line:** claim before the work, not after, because during the payment await the
first request has stored nothing to find.

---

## 7. Money and rounding rules

See [D1]–[D4]. In brief: Python `int`, minor units, `_minor` suffix on every field. `float` is banned
in every money path. Exactly one rounding site in the system — the order-level discount — using
`Decimal` with `ROUND_HALF_EVEN`, immediately converted back to `int`, then clamped with
`min(discount, gross)` so **I9** holds unconditionally. `Decimal` appears in one module and never
escapes it. No client-supplied money value is accepted anywhere in the API.

---

## 8. Error-model choices

See [D29], [D9], [D17]. Every failure returns
`{"error": {"code", "message", "details"}}` with a stable machine-readable code. **One code maps to
exactly one HTTP status** — which is why the add-time and checkout-time inventory failures are two
codes rather than one code with two statuses. `DomainError` subclasses carry their own code and
status, and the API layer maps them generically without switching on the code, so the two layers
cannot desynchronise. FastAPI's default Pydantic `422` body is normalised into the same envelope.
Errors are distinguishable **by cause the caller can act on** — deliberately not by cause the caller
cannot act on, which is why wrong-owner and unknown coupon are indistinguishable [D17]. Full
catalogue: `TAD.md` §7.

---

## 9. Implemented vs deferred

**Build record.** 17 tickets, 20 commits (commit 0 is the planning documents), **one rework in the
entire build** (B5), zero `BLOCKED` escalations, zero cuts. 1,906 lines under `src/`. 687 tests green
in ~14 s. Every ticket in `FTL.md` shipped, including both marked cuttable — the threadpool weakness
demonstration (C9) and the demo harness (C11, 254 lines against a 400-line budget).

**QA record.** After the build, a separate session ran **67 manual scenarios against the running
service** — every endpoint, the README flow verbatim, the demo harness's concurrency panels, and the
env-var configuration — comparing response *bodies*, not just status codes. It found **one code bug**:
`SAD.md` §3 documented the `Idempotency-Key` header as at most 128 characters and the router did not
enforce it, so a 200-character key produced an order. Fixed with a one-line `max_length` on the header
declaration and two tests in `tests/test_validation.py` (689 green after). It found **one doc bug**:
TAD §7's closing sentence understated which errors carry an identifier in `details`; corrected, no
behaviour changed. The critic had missed the first because it gated against `FTL.md` definitions of
done, and the length bound lived only in `SAD.md` — a gap *between* two documents, not in either.

**Implemented.** All fifteen invariants in §1, each with the test named there. The full HTTP surface of
`TAD.md` §8 (13 routes), with OpenAPI generated at `/docs`. Ordered locking through `LockManager`,
plus a test that bypasses it and shows the invariant breaking
(`tests/test_concurrency_inventory.py::test_lock_is_load_bearing_bypass_breaks_i2`) — the lock is
demonstrated load-bearing, not asserted. Idempotency in all four scenarios of [D19]–[D23] plus scope,
twelve tests. The coupon lifecycle with release-on-failure, including survival across *repeated*
failed checkouts. Per-order half-even money. The reconciling report exposing `reserved`. The
threadpool demonstration. The demo harness.

**Where the build corrected the plan** — three places, all recorded rather than papered over:

1. **`COUPON_IN_USE` is unreachable in-process.** The losers of a coupon race receive
   `422 COUPON_ALREADY_REDEEMED`. Full reasoning in [D39]; PRD, TAD, SAD, FSD and FTL corrected.
2. **The demo harness has no "force payment failure" row.** It would need a per-request gateway
   switch — a test hook in production code. **I5** and **I13** are proven by
   `tests/test_release_on_failure.py` instead of shown in a browser. Cost to restore: a test-only
   endpoint behind the admin token, ~20 min; judged not worth a production-code hook.
3. **Framework-level 404 (unknown path) and 405 (wrong method) keep Starlette's default body.** The
   catalogue has no code for them, and inventing `ROUTE_NOT_FOUND` for a condition the domain never
   raises was judged not worth it. Every domain and validation failure uses the envelope. Cost: two
   handlers, ~10 min.

**Tests — what ships, and why there are two kinds.** `tests/` holds the **90 business-rule tests**:
money, idempotency, inventory concurrency, coupon concurrency, release-on-failure, price drift, report
reconciliation, the `CART_MODIFIED` window, validation, and the threadpool demonstration. Those are
the tests the spec's "small number of meaningful tests" refers to, and every one names the invariant
it protects. `tests/gates/` holds **202 per-ticket build gates** — signature diffs against `TAD.md`
§3, schema strictness, router status codes, app-factory singletons, per-module smoke checks — written
by the critic session as the definition-of-done check for each ticket. They are kept, segregated,
because they are the evidence that every ticket was gated, and because deleting tests to look leaner
is worse than explaining them. A reviewer who wants only the meaningful tests runs
`pytest tests --ignore=tests/gates`.

**Deliberately deferred, with the cost to close:**

| Deferred | Why | Cost to close |
|---|---|---|
| **Timeout on the payment call** | The fake gateway never hangs; a real one would hold the product and ledger locks for its full duration (`SAD.md` A8) | ~20 min. **Highest value/effort ratio here** |
| Idempotency record TTL and eviction | Unbounded growth is irrelevant at demo scale; it is nonetheless a leak | ~30 min. Mandatory before production |
| Bounded retry instead of `409 CART_MODIFIED` | Detected and refused, not retried; pushes a narrow race onto the client (§12) | ~30 min |
| Test-only gateway switch (restores the harness row) | A test hook in production code | ~20 min |
| Envelope for framework 404/405 | The domain never raises them | ~10 min |
| Authentication; ownership checks on `GET /carts`, `GET /orders` | Out of scope per spec; checking an unverified claim is fake access control | 2–3h; trivial after authn |
| Coupon expiry | [D18] | ~45 min including a clock seam |
| Minimum order value for milestone eligibility | Blunts coupon farming (`SAD.md` A1); a product decision the spec does not make | ~10 min |
| Rate limiting / velocity limits | Does not fit the timebox | ~1h in-process |
| Persistence and multi-instance | Spec permits in-memory | §10 |
| Audit log of admin actions | No logging infrastructure in scope | ~30 min |
| Multi-currency, tax, refunds, cancellation, partial fulfilment, pagination | Not in the spec | — |

No ticket was cut and nothing planned is unimplemented.

---

## 10. How this evolves for multiple instances and production scale

Full mapping in `TAD.md` §9. The three points that matter:

**Most of this machinery disappears.** With a real database and one transaction per checkout, the
reservation lifecycle collapses into `ROLLBACK`, and the compensating-release code deletes itself. The
in-memory design is *more* complex than the production one, not less, precisely because it has no
transactions. Reservations become necessary again only if payment moves *outside* the transaction —
at which point this becomes a saga with a compensating release and a reaper for expired reservations,
and that is the real design fork at scale.

**Mutual exclusion becomes compare-and-swap.** `UPDATE products SET available = available - :q WHERE
id = :id AND available >= :q` needs no lock at all; **I1** falls out of the `WHERE` clause. Likewise
`UPDATE coupons SET state='RESERVED' WHERE code=:c AND state='AVAILABLE'` — rowcount 1 wins the race,
0 loses, and **I4** becomes a constraint rather than application logic. **I7** becomes
`UNIQUE(milestone)`. The lock *ordering* rule survives, because row locks deadlock too; Postgres
detects and aborts one transaction, so retry-on-deadlock (`40P01`) must be added.

**What breaks first, in order.** (1) `IdempotencyRegistry` — a second instance knows nothing about the
first's in-flight key, so duplicate orders appear immediately. (2) `last_rewarded_milestone` — two
admin calls on two instances both reward milestone *k*. (3) The lock-free report, whose correctness
argument depends on single-process synchronous commits and needs `REPEATABLE READ`. (4) The product
locks. Failures 1–3 are **silent** correctness failures and 4 is loud, which is why idempotency and
the coupon ledger are the first two things to move into the database. A new concern appears that
cannot exist in-process: a **stale claim**, where an instance crashes mid-checkout and leaves a key
claimed forever, requiring a TTL on `IN_PROGRESS` records.

---

## 11. How AI was used

> **⚠️ VERIFY AND EDIT BEFORE SUBMISSION.** This is drafted from what actually happened during
> planning. It is your account and must be in your words.

AI was used throughout: for planning documents, for implementation against frozen interfaces, and for
test scaffolding. The design decisions in §4 are mine; AI was used to pressure-test them and to
surface options I had not considered.

**Where AI output was materially redirected — the stack decision.** Asked to choose a stack, the model
initially recommended TypeScript/Node on the grounds that the FAQ names it as the house stack and the
reviewer would read it most fluently. I rejected that recommendation and required it be re-argued from
two constraints it had underweighted: the 4–6 hour timebox, and my actual background — five years of
production FastAPI, no production Node backend, no Go. The recommendation reversed, and the deciding
argument turned out to be one neither of us had stated initially: **the house-stack advantage inverts
when you are not fluent in it**, because a Node reviewer reading first-time Express judges the
Express rather than the reasoning. That argument, not the original one, is what appears in [D27].

**Where I required verification rather than assertion.** The concurrency design rests on the claim
that asyncio has the same yield-point semantics as Node and that FastAPI's `def` handlers run in a
threadpool. Rather than accept these, I had them verified empirically before the design was frozen.
The results confirmed the core claim — an unlocked critical section spanning an `await` oversold 99 of
100 — but also **refined** a claim I had made myself. The claim — that `def` handlers give real thread preemption — was right, but the obvious demonstration of it was not: a pure-CPU threaded race does *not* reproduce
at Python's default 5 ms switch interval and needs `sys.setswitchinterval` lowered to force it. A test
built on that would have been contrived and possibly flaky. The threadpool demonstration test [D26]
was consequently redesigned around blocking I/O in the critical section, which oversells 95 of 100 at
default settings and is deterministic. This is the clearest case where insisting on verification
changed the design rather than merely confirming it.

**Where AI was constrained.** Implementation ran as three sessions with disjoint file ownership
against interfaces frozen before any code was written ([D38]): an orchestrator that wrote no code and
committed only on a PASS, a worker that owned `src/` and could not touch `tests/`, and a critic that
owned `tests/`, could not touch `src/`, and gated every ticket against the documented definition of
done — including running the invariant greps literally and refusing on any `await` inside a lock
scope other than the payment call. `CLAUDE.md` encodes the
invariants, the lock ordering, the money rule, and the ownership boundaries, and is loaded by every
session. The intent was that the design not drift under generation — the interfaces were the contract,
not a suggestion. Seventeen tickets, one rework, zero blocked escalations, zero cuts.

**Where the critic gate caught a real bug.** One rework in seventeen tickets. B5's admin-token check
used `secrets.compare_digest` on `str` values, which raises `TypeError` when the supplied token
contains non-ASCII characters — turning a `403 FORBIDDEN` into a `500`. The critic sent a non-ASCII
token, got the 500, and returned a FAIL naming the file and line; the worker fixed it by comparing
bytes. Neither I nor the orchestrator would have thought to try that input.

**Where the build corrected the plan.** The plan — written with AI, every ambiguity resolved by me —
specified that the loser of a concurrent coupon race receives `409 COUPON_IN_USE`. The critic could not
produce that outcome and worked out why: the ledger lock is held across the payment await, so no
competitor ever observes `RESERVED`. The orchestrator ruled to keep the code as built and flagged the
ruling for me; I ratified it after the build with the reasoning in [D39]. If asked where AI-generated
*design* was wrong, this is the example: the planning session specified an error state that the
design it had itself described cannot produce, and a differently-prompted session caught it by testing
rather than by reading.

**Where a second, differently-scoped session caught what the first missed.** The critic gated every
ticket against `FTL.md` and never failed the `Idempotency-Key` length bound, because that bound was
written only in `SAD.md` §3 and no ticket's definition of done referenced it. A post-build QA session,
prompted to test the *running service against every document* rather than the code against its
tickets, found it in scenario B15 within minutes. The lesson I would carry forward: a gate is only as
complete as the documents it is told to read.

**What I did not do:** accept generated concurrency code without reasoning about its yield points. The
single-`await`-per-critical-section rule ([D24]) exists partly for this reason — it makes the property
auditable with `grep` rather than by trusting a reading of the code.

---

## 12. What I would examine first with another two hours

In priority order.

1. **Put a timeout on the payment call (~20 min).** The only weakness here that degrades the service
   with no attacker involved. The gateway is awaited while holding the product and coupon-ledger
   locks, so a hung gateway blocks every checkout for those products indefinitely. `asyncio.wait_for`
   plus a compensating release. Highest value for the effort on the entire list.
2. **Property-test the money and report invariants (~40 min).** `I12` (`gross - discount == net`) and
   `I15` (`generated == available + reserved + redeemed`) are currently asserted against
   hand-constructed scenarios. Generating random sequences of operations and asserting the identities
   after each one would test them far harder than the example-based tests do, and would most likely
   find something.
3. **Close the `CART_MODIFIED` window properly (~30 min).** The optimistic read-then-verify in
   `TAD.md` §4.4 currently *detects* a cart mutating between the unlocked read and lock acquisition
   and returns `409`. A bounded retry loop would be better than pushing it onto the client, and the
   current behaviour is the least satisfying part of the design.
4. **Move `IdempotencyRegistry` behind an interface and write a SQL-backed implementation (~40 min).**
   It is the first thing to break under multiple instances (§10), and implementing it against
   `UNIQUE(cart_id, key)` would validate that the whole idempotency model actually survives the
   migration rather than only claiming it does.
5. **Load-test the lock ordering.** Everything about deadlock freedom here is an argument from the
   total order, not an observation. A few thousand randomised concurrent operations across
   overlapping product sets would turn that argument into evidence — which is the one thing this
   submission genuinely lacks by choosing Python over Go ([D27]).

---

## 13. Time spent

Approximately **4 hours wall-clock**, in four phases:

- **~2 h planning**, before any code: spec analysis, four rounds of ambiguity resolution, and the
  seven design documents (`CLAUDE.md`, `PRD.md`, `TAD.md`, `SAD.md`, `FSD.md`, `FTL.md`, this file).
- **~1 h 15 m build**: first ticket issued 15:45 IST on 10 Sep; the halfway checkpoint (A3) at 16:33;
  everything through B7 and C9 by roughly 16:50; the C5 ruling and the demo harness completed after
  an overnight pause, finishing 01:06 IST on 11 Sep.
- **~20 m** post-build verification against the plan, the [D39] ratification, and these sections.
- **~20 m manual QA**: 67 scenarios against the running service; one code bug and one doc bug found,
  fixed with tests, and pushed (§9).

The build ran as three concurrent sessions — orchestrator, worker, critic — so machine time exceeds
wall-clock. The figure declared is wall-clock. No ticket was cut and nothing is incomplete.
