# CLAUDE.md — Build Rules

Authoritative for every session and subagent. If this conflicts with your prompt, **this wins**.
Spec: `be/README.md`. Grading weight is in `DECISIONS.md`, not code volume. Timebox 4–6h.

Stack: Python 3.11+ / FastAPI / Pydantic v2 / pytest + pytest-asyncio + httpx. [D27]
No database, no auth, no real payments, no multi-currency. These are non-goals, not TODOs.

## Invariants — every test names the invariant it protects

- **I1** `stock_total == available + reserved + sold` for every product, always. All three ≥ 0. Never oversell.
- **I2** A cart is checked out at most once. Second attempt → `409 CART_ALREADY_CHECKED_OUT`.
- **I3** One `(cart_id, Idempotency-Key)` yields **at most one order** under any interleaving, including concurrent arrival.
- **I4** A coupon is redeemed at most once, by at most one order.
- **I5** A coupon is never consumed by a checkout that does not produce an order. Any failure after reservation returns it to `AVAILABLE`.
- **I6** A coupon is redeemable only by the customer it is bound to.
- **I7** At most one coupon exists per milestone. Milestone *k* is rewarded once, ever.
- **I8** Only orders in state `PLACED` increment the order counter.
- **I9** `0 <= discount_minor <= gross_minor`. Order total is never negative.
- **I10** An order is immutable and carries its own price snapshot. Later product mutation never changes a past order.
- **I11** The report is a pure read. It never mutates state; two identical calls return identical bodies.
- **I12** `gross_minor - discount_minor == net_minor` exactly, per order and in report totals.
- **I13** Inventory reserved by a failed checkout is released in full.
- **I14** All money is integer minor units. No float ever touches a money value.
- **I15** `coupons.generated == available + reserved + redeemed`, always.

## Money [D1] [D2] [D3] [D4]

- Type: Python `int`, **minor units** (paise). Every money field name ends in `_minor`.
- `float` is **banned** in every money path. No `/` on money. No `round()` on a float.
- Rounding happens **exactly once per order**, on the order-level discount: `Decimal` + `ROUND_HALF_EVEN` → `int`.
- Clamp: `discount_minor = min(discount_minor, gross_minor)`.

## Lock acquisition order — ABSOLUTE [D24]

```
products (ascending product_id)  ->  coupon ledger  ->  cart
```

Never any other order. `asyncio.Lock` is **not reentrant** — never acquire a lock you already hold.
Sort product ids before locking; never lock in cart-insertion order.
Acquire only through `LockManager.acquire(...)`, which enforces the order by construction. Do not
acquire a raw lock directly.

## Atomicity rule — the greppable one [D24] [D25]

**All state mutation is written as synchronous functions. Only lock acquisition and the payment
call are `await`ed.** A synchronous block cannot yield, so it cannot interleave.
There must be **exactly one `await` inside the checkout critical section**: the payment gateway.
Methods suffixed `_locked` are synchronous and require the caller to already hold the relevant lock.

## Errors [D29]

Every failure returns:

```
HTTP <status>   {"error": {"code": "<STABLE_CODE>", "message": "...", "details": {...}}}
```

- `code` is a stable constant from the TAD §7 catalogue. Never reword it. Never invent a new one.
- One code maps to exactly one HTTP status.
- Never reveal that another customer's coupon exists: not-owned and nonexistent both → `422 COUPON_INVALID`. [D17]
- FastAPI's default Pydantic `422` body **must** be normalised into the envelope above.

## File ownership — orchestrator / worker / critic [D38]

```
ORCH    UNIBLOX-ORCH     .orchestration/PROGRESS.md + git commits.   Writes NO code, ever.
WORKER  UNIBLOX-WORKER   src/**, pyproject.toml, README.md, web/**   all FTL A* and B* tickets
CRITIC  UNIBLOX-CRITIC   tests/**                                    all FTL C* tickets + the review gate
```

In `FTL.md`, "Session A/B/C" is a *ticket family*, not a session: A* and B* → WORKER, C* → CRITIC.
Loop protocol and message formats: `.orchestration/PROTOCOL.md`.

**Do not edit files outside your assigned ownership.** If you need a change in another session's
file, that is a `BLOCKED` message to ORCH, not a workaround. Never duplicate code into your own tree.

## Frozen interfaces

The signatures in `TAD.md` §3 are **frozen**. Do not change one, do not add a parameter, do not
rename. If a signature is wrong, stop and say so — do not adapt around it locally.

## Discipline

- Every decision is tagged `[D#]` from `DECISIONS.md`. Do not renumber, do not reuse.
- Dependencies are exactly: `fastapi`, `uvicorn`, `pydantic`, `pytest`, `pytest-asyncio`, `httpx`. Add nothing.
- Do not add auth, persistence, caching, metrics, rate limiting, retries, or logging frameworks.
- Every handler touching shared state is `async def`. A `def` handler runs in a threadpool where
  `asyncio.Lock` does not protect the invariants. [D26]
- `pyproject.toml` must set `asyncio_mode = "auto"` or every async test silently skips.
