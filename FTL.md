# FTL — Feature Ticket List

Three parallel Claude Code sessions with **disjoint file ownership**. `CLAUDE.md` is the shared
contract; `TAD.md` §3 is the frozen interface. No session edits another's files. [D38]

**Sizing basis.** Estimates assume a *supervised* session working from the frozen interfaces — they
are review-and-verify time, not typing time. They are not human-coding estimates.

**Honest accounting.** Person-hours below total ≈ **16h** across three sessions. Wall-clock target is
**≈5h**, achieved by parallelism, and only if the cut list is used. When declaring time spent in the
submission, **declare wall-clock and say it was parallelised** — do not report 5h as though it were
5 person-hours.

**Session mapping.** The build runs as ORCH / WORKER / CRITIC (see `CLAUDE.md`, `[D38]`). The
"Session A/B/C" labels below are **ticket families**, kept so the ownership boundaries and the
critical path stay legible: **A\* and B\* tickets → WORKER**, **C\* tickets → CRITIC**, and ORCH owns
none of them. Build order under one worker is API-surface-first — `A1, B1–B6, A2–A8, B7` — so the app
boots against stubs by ticket 7 and every later core ticket gets an end-to-end gate. The exact
sequence lives in `.orchestration/PROGRESS.md`.

---

## Phase 0 — the blocking ticket

Nothing starts until this lands. B and C are idle until it does, so it is the highest-urgency item
in the build.

### `A1` — Skeleton, config, seed data, and frozen stubs · **Session A** · 40m · **BLOCKING**

- **Files:** `pyproject.toml`, `src/config.py`, `src/core/*.py` (all, as stubs), `.gitignore`
- **Exposes:** every signature in `TAD.md` §3, as stubs raising `NotImplementedError`.
  `config.py` complete and working. `SEED_PRODUCTS` (6, per PRD 2.1) and `SEED_CUSTOMERS` (5).
- **Consumes:** `TAD.md` §3, `CLAUDE.md`
- **DoD:** `pytest` collects zero tests without import errors. `python -c "import src.core.checkout"`
  succeeds. `pyproject.toml` sets `asyncio_mode = "auto"`. **Every §3 signature exists verbatim** —
  argument names, keyword-only markers, and return types included.
- **Protects:** the interface contract itself. A signature that drifts here silently breaks B and C.
- **Note:** stubs first, implementation later, is not busywork — it is what makes three-way
  parallelism possible at all.

---

## Session A — `src/core/**`

### `A2` — Money · 30m · dep: A1
- **Files:** `src/core/money.py`, `src/core/errors.py`
- **Exposes:** `line_total_minor`, `gross_minor`, `discount_minor`, `net_minor`, `ErrorCode`, `DomainError` + subclasses
- **DoD:** `discount_minor` uses `Decimal` + `ROUND_HALF_EVEN`, clamps to gross, rejects `percent`
  outside `[0,100]`. `grep -rn "float\|[^_]/ \|round(" src/core/money.py` returns only the `Decimal`
  quantize. One `DomainError` subclass per `ErrorCode`, each with a fixed `http_status`.
- **Protects:** **I9 I12 I14** · [D1] [D2] [D3] [D4] [D29]

### `A3` — Store and locks · 45m · dep: A2 · **CRITICAL PATH**
- **Files:** `src/core/models.py`, `src/core/store.py`, `src/core/locks.py`
- **Exposes:** all dataclasses; `InMemoryStore` with `available`/`reserved`/`sold` and the four
  `*_locked` mutators; `LockManager.acquire`
- **DoD:** `LockManager.acquire` acquires **products (sorted) → coupon ledger → cart** and releases
  in reverse; there is no other way to obtain a lock. Every `*_locked` method is `def`, not
  `async def`. `stock_total == available + reserved + sold` holds after every mutator.
- **Protects:** **I1 I10** · [D24] [D33]

### `A4` — Idempotency registry · 40m · dep: A2
- **Files:** `src/core/idempotency.py`
- **Exposes:** `ClaimStatus`, `ClaimResult`, `IdempotencyRegistry.{claim,complete,release,fingerprint}`
- **DoD:** `claim` is **synchronous** — `grep "await" src/core/idempotency.py` returns nothing.
  Fingerprint mismatch is checked **before** the in-progress check. `fingerprint` is stable across
  key ordering. `release` fully removes the record.
- **Protects:** **I3** · [D19] [D20] [D21] [D22]

### `A5` — Payments and CartService · 45m · dep: A3
- **Files:** `src/core/payments.py`, `src/core/carts.py`
- **Exposes:** `PaymentGateway`, `FakePaymentGateway`, `AlwaysDeclineGateway`; `CartService` (7 methods), `CartView`, `CartLineView`
- **DoD:** `FakePaymentGateway.charge` awaits `asyncio.sleep` **even at latency 0** — without that
  await the critical section is trivially atomic and the whole concurrency design is untestable.
  Cart mutators acquire `products=[pid]` then `cart_id`, never cart-first. `add_item` on an existing
  line increments and re-snapshots. `reprice` re-snapshots every line. Mutating a `CHECKED_OUT` cart
  raises.
- **Protects:** **I2 I10** · [D5] [D6] [D7] [D8] [D9] [D25]

### `A6` — CouponService · 40m · dep: A3
- **Files:** `src/core/coupons.py`
- **Exposes:** `GenerationResult`, `CouponService.{generate,list_coupons,reserve_locked,commit_locked,release_locked}`
- **DoD:** `generate` rewards `k = last_rewarded + 1` iff `placed >= k*n`, binds to
  `store.orders[k*n - 1].customer_id`, returns `pending_milestones`, raises `NoEligibleMilestone`
  otherwise. `reserve_locked` returns **identical** `CouponInvalid` for unknown-code and wrong-owner.
  The three `*_locked` methods are `def`, not `async def`. Codes are `uuid4`-derived, never
  milestone-derived.
- **Protects:** **I4 I5 I6 I7 I15** · [D12] [D14] [D17] [D18] [D30] [D37]

### `A7` — CheckoutService · 75m · dep: A4, A5, A6 · **CRITICAL PATH**
- **Files:** `src/core/checkout.py`
- **Exposes:** `CheckoutResult`, `CheckoutService.checkout`
- **Consumes:** everything above
- **DoD:** implements `TAD.md` §4.5 exactly. `claim` runs **before** any lock. **Exactly one `await`
  inside the lock scope** — verify with `grep -n await src/core/checkout.py`; any second one is a bug.
  Optimistic read → lock → re-verify, raising `CART_MODIFIED` on drift. The `except` path releases
  inventory, releases the coupon, and releases the idempotency record, in that order, before
  re-raising. The commit block contains no `await`.
- **Protects:** **I1 I2 I3 I5 I9 I13** · [D11] [D13] [D19]–[D25]
- **Note:** the single highest-risk ticket in the build. If anything slips, it slips here.

### `A8` — ReportService · 30m · dep: A3, A6
- **Files:** `src/core/reports.py`
- **Exposes:** `Report`, `ItemsPurchased`, `ReportService.build`
- **DoD:** takes **no locks**. Mutates nothing — call it twice, get identical output.
  `gross - discount == net`; `generated == available + reserved + redeemed`, including while a
  checkout is mid-flight. Counts only `PLACED` orders.
- **Protects:** **I11 I12 I15** · [D31]

---

## Session B — `src/api/**`, `src/main.py`

Session B can begin as soon as **A1** lands, coding against stubs. It does not wait for A's
implementations.

### `B1` — Error envelope and handlers · 45m · dep: A1
- **Files:** `src/api/errors.py`
- **Exposes:** `DomainError` → HTTP handler; `RequestValidationError` → `422 VALIDATION_FAILED`; catch-all → `500 INTERNAL_ERROR`
- **DoD:** every error body is `{"error": {"code","message","details"}}`. FastAPI's default Pydantic
  `422` shape **does not appear anywhere** — verify by POSTing a bad body. No stack trace, file path,
  or internal type name in any response. The `DomainError` handler reads `code`/`http_status` off the
  exception and **never switches on the code**.
- **Protects:** the error contract · [D29]

### `B2` — Pydantic schemas · 40m · dep: A1
- **Files:** `src/api/schemas.py`
- **DoD:** every request model sets `extra="forbid"` and `strict=True`. Quantities are
  `Field(gt=0, le=100)`. **No money field is ever accepted from a client.** Response models mirror
  `TAD.md` §3 view objects exactly, all money as `int` with `_minor` suffixes.
- **Protects:** **I14** · [D32] [D36]

### `B3` — Products and carts routers · 50m · dep: B1, B2
- **Files:** `src/api/routers/products.py`, `src/api/routers/carts.py`, `src/api/deps.py`
- **DoD:** all seven cart/product routes per `TAD.md` §8, with the documented status codes
  (`201` on create/add, `204` on delete). Every handler is `async def` — a `def` handler runs in a
  threadpool where `asyncio.Lock` does not protect the invariants. [D26]
- **Protects:** **I2** · [D6] [D9]

### `B4` — Checkout and orders routers · 45m · dep: B3
- **Files:** `src/api/routers/orders.py`, checkout route in `carts.py`
- **DoD:** `Idempotency-Key` header required → `400 IDEMPOTENCY_KEY_REQUIRED` when absent.
  `CheckoutResult.replayed` maps to **`200` + `Idempotent-Replay: true`**; a fresh checkout maps to
  **`201`**. The route passes the key straight through and **never computes the fingerprint itself** —
  that lives in `CheckoutService` so it cannot drift.
- **Protects:** **I3** · [D21] [D23]

### `B5` — Admin router and token guard · 35m · dep: B3
- **Files:** `src/api/routers/admin.py`
- **DoD:** all three admin routes behind one `X-Admin-Token` dependency → `403 FORBIDDEN` when
  missing or wrong. `POST /admin/coupons` returns `201` with the coupon plus `pending_milestones`;
  `409 NO_ELIGIBLE_MILESTONE` otherwise. `GET /admin/report` is `GET` and mutates nothing.
- **Protects:** **I7 I11** · [D14] [D28]

### `B6` — App wiring · 30m · dep: B4, B5
- **Files:** `src/main.py`
- **DoD:** app factory builds one `InMemoryStore`, one `LockManager`, one `IdempotencyRegistry`, and
  the services as **singletons** — a per-request store would silently void every invariant. OpenAPI
  title/description/tags set. `web/` mounted at `/demo` if it exists. `uvicorn src.main:app` runs.
- **Protects:** all of them — shared state must actually be shared · [D27]

### `B7` — README, run instructions, API examples · 45m · dep: B6
- **Files:** `README.md`
- **DoD:** clone-to-running in under five commands. A `curl` example for every endpoint including
  error cases. States that `/docs` is the generated OpenAPI document. Documents `n`, `x`, and the
  admin token as env vars. Names which operations are administrative. States approximate time spent.
- **Protects:** the "repeatable setup" deliverable · [D35]

---

## Session C — `tests/**`, `web/**`

Session C can begin as soon as **A1** lands. Write tests against the stubs; they fail with
`NotImplementedError` until A lands each module, which is the correct red state.

### `C1` — Test harness · 40m · dep: A1
- **Files:** `tests/conftest.py`
- **Exposes:** `app_client` (`httpx.AsyncClient` + `ASGITransport`), `fresh_store`, `declining_gateway`, `seeded_products`
- **DoD:** each test gets a **fresh store** — leaked state between concurrency tests produces
  flakiness that looks like a race and wastes hours. `asyncio_mode = "auto"` verified working by a
  trivial async test that actually runs (a silently skipped async suite is the worst failure mode
  available here). `ASGITransport` drives the app in-process on the test's own event loop.
- **Protects:** every test below

### `C2` — Money and rounding · 30m · dep: A2
- **Files:** `tests/test_money.py`
- **DoD:** half-even at both `.5` boundaries (`round-half-to-even` differs from half-up — assert a
  case where they disagree). Clamp at gross. `percent=0` and `percent=100`. `gross - discount == net`
  as a property over a table of cases. No float anywhere in the assertions.
- **Protects:** **I9 I12 I14** · [D2] [D3] [D4]

### `C3` — Concurrent oversell · 50m · dep: A7, B4 · **CRITICAL PATH**
- **Files:** `tests/test_concurrency_inventory.py`
- **DoD:** N=20 concurrent checkouts, distinct keys, against `prd_headset` (stock 1): **exactly one
  `201`**, nineteen `409 INSUFFICIENT_INVENTORY`, `sold == 1`, `available == 0`, `reserved == 0`.
  A second case with stock 3 and N=20 asserts exactly three. **Includes a test that fails when the
  `LockManager` is bypassed** — otherwise the suite cannot prove the lock is load-bearing.
- **Protects:** **I1** · [D24]

### `C4` — Idempotency storm · 55m · dep: A7, B4 · **CRITICAL PATH**
- **Files:** `tests/test_idempotency.py`
- **DoD:** four scenarios.
  (a) N=10 concurrent, one key: exactly one `201`, nine `409 REQUEST_IN_PROGRESS`, **one order in the
  store**, inventory decremented once.
  (b) sequential replay after completion: `200`, `Idempotent-Replay: true`, byte-identical order body.
  (c) same key, different `coupon_code`: `409 IDEMPOTENCY_KEY_REUSED`.
  (d) retry after a *failed* checkout with the same key: proceeds as a new attempt, not a cached
  failure.
  Payment latency must be non-zero so the in-flight window is real.
- **Protects:** **I3** · [D19] [D20] [D21] [D22] [D23]

### `C5` — Coupon race · 45m · dep: A7, A6
- **Files:** `tests/test_coupons_concurrency.py`
- **DoD:** N concurrent checkouts with one coupon: exactly one `201`, rest `409 COUPON_IN_USE`, final
  state `REDEEMED` with exactly one `redeemed_by_order_id`. N concurrent `POST /admin/coupons` at one
  milestone: exactly one coupon created. Wrong-owner returns a body **byte-identical** to unknown-code.
- **Protects:** **I4 I6 I7** · [D12] [D17]

### `C6` — Release on failure · 40m · dep: A7, A5
- **Files:** `tests/test_release_on_failure.py`
- **DoD:** with `AlwaysDeclineGateway`: `402 PAYMENT_DECLINED`; coupon back to `AVAILABLE`; `reserved
  == 0` for every product; `available` restored to its pre-checkout value; **no order created**; the
  idempotency record released so the same key can retry. Also asserts the coupon survives *repeated*
  failed checkouts — the spec's "a coupon must not be lost" is about the repeated case.
- **Protects:** **I5 I13** · [D22] [D30]

### `C7` — Price drift and reprice · 30m · dep: A5, A7
- **Files:** `tests/test_price_drift.py`
- **DoD:** mutate a seeded price after add-to-cart → checkout returns `409 PRICE_CHANGED` with
  `details.changed[]` naming the product and both prices; nothing reserved, nothing consumed;
  `reprice` then checkout succeeds at the **new** price; the order records the new price.
- **Protects:** **I10** · [D5] [D6]

### `C8` — Report reconciliation · 35m · dep: A8, B5
- **Files:** `tests/test_report.py`
- **DoD:** after a mixed run (plain orders, discounted orders, failed checkouts), the report
  reconciles against `GET /orders/{id}` for every order: item quantities, gross, discount, net.
  `gross - discount == net`. `generated == available + reserved + redeemed`. Two consecutive calls
  return identical bodies. Failed checkouts contribute nothing.
- **Protects:** **I8 I11 I12 I15** · [D15] [D31]

### `C9` — Threadpool weakness demonstration · 30m · dep: A7 · **CUTTABLE**
- **Files:** `tests/test_threadpool_weakness.py`
- **DoD:** drives `CheckoutService` from a `ThreadPoolExecutor` and **demonstrates the invariant
  breaking**, because `asyncio.Lock` is not thread-safe. Must be **deterministic, not flaky**: use a
  blocking sleep in the critical section rather than tuning `sys.setswitchinterval` — verified to
  oversell 95/100 at default settings, whereas a pure-CPU race needs interpreter tuning and would
  read as contrived. Docstring states plainly that this is a *documented limitation* of the design,
  and that it is why every state-touching handler is `async def`. [D26]
- **Protects:** documents the boundary of **I1**'s guarantee

### `C10` — Validation and edge cases · 30m · dep: B2, B3 · **CUTTABLE**
- **Files:** `tests/test_validation.py`
- **DoD:** quantity `0`, `-1`, `2.5`, `"5"`, `10**500`, `101` all rejected at the edge. Unknown
  product `404`. Unknown customer `422`. Empty-cart checkout `422 CART_EMPTY`. Double checkout `409`.
  Missing `Idempotency-Key` `400`. Missing admin token `403`. Unknown request field rejected.
- **Protects:** **I1 I2 I14** · [D13] [D32] [D36]

### `C11` — Demo harness · 60m · dep: B6 · **CUTTABLE**
- **Files:** `web/index.html`
- **DoD:** per `FSD.md`. Four panels, raw status histograms, invariant markers computed from API
  responses. Under ~400 lines.
- **Protects:** nothing — it makes invariants *visible*, it does not enforce them · [D34]

---

## Critical path

```
A1 ──► A3 ──► A7 ──► C4
 │      │      ▲      ▲
 │      │      │      └── the test that proves I3, the least obvious invariant
 │      │      └───────── the critical section itself; highest-risk ticket
 │      └──────────────── locks and the inventory counters
 └─────────────────────── blocks all three sessions
```

`A1 → A3 → A7 → C4` ≈ **3h 40m** of strictly serial work. Everything else has slack.

**Schedule risk, in order:**
1. **A7 overruns.** It is design-dense and everything downstream waits on it. If A7 is not started by
   the halfway mark, cut from the list below immediately.
2. **A1 ships a signature that differs from `TAD.md` §3.** B and C then build against a contract that
   does not exist, and the integration fails late. A1's DoD is deliberately pedantic about this.
3. **`asyncio_mode` misconfigured.** Every async test silently skips and the suite reports green with
   nothing running. C1 exists partly to catch this.
4. **Session B or C edits a file it does not own.** Produces a merge conflict that surfaces at the
   worst possible moment. `CLAUDE.md` is the only guard.

---

## CUT LIST — drop in this order

Cut from the top the moment the timebox is threatened. Everything above the line survives.

| # | Cut | Cost of cutting |
|---|---|---|
| 1 | `C11` demo harness (and `FSD.md` entirely) | Behaviour is proven by tests, just not visible in a browser. The spec says frontend cannot compensate for a weak backend — so it is the first thing that should go. |
| 2 | `GET /admin/coupons` endpoint | Report still reports coupon **counts**. Only the harness and manual inspection lose. Drop from `B5`. |
| 3 | `C9` threadpool demonstration | Lose a *demonstrated* answer to "identify a weakness". Keep the paragraph in `DECISIONS.md` — the reasoning survives even if the test does not. |
| 4 | `C10` validation tests | Pydantic is doing the work declaratively and it is visible in the OpenAPI schema. Lowest-yield tests in the suite. |
| 5 | `POST /carts/{id}/reprice` + `C7` | Falls back to re-`PUT`ting a quantity to re-snapshot. Reverses [D6] — **document the reversal in `DECISIONS.md`**, do not leave `PRICE_CHANGED` as a dead end with no escape. |
| 6 | `CART_MODIFIED` re-verification (TAD §4.4) | Accepts a narrow race where a cart mutates between the unlocked read and lock acquisition. Small window, real risk. **Document it as a known gap**, do not delete it silently. |
| 7 | `C8` report reconciliation test | The report becomes untested. Uncomfortable — the spec asks for reconciliation explicitly. Cut only under real pressure. |
| — | **LINE — nothing below is cuttable** | |
| — | `A1`–`A8`, `B1`–`B7`, `C1`–`C6` | These are the assignment. `C3`, `C4`, `C5`, `C6` are the four tests the spec's grading criteria point at directly. |

**Never cut:** `DECISIONS.md`. It is the heaviest-weighted deliverable. If the choice is between one
more test and finishing `DECISIONS.md`, finish `DECISIONS.md` — and write down which test you cut and
why, because that itself is a decision worth recording.
