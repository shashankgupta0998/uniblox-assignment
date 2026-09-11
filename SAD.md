# SAD — Security & Abuse Model

Authentication and authorization are out of scope per `be/README.md`. This document therefore does
**not** design access control. It describes the trust boundary that exists in its absence, the abuse
cases against each invariant, the validation that *is* enforced, and what is deferred.

---

## 1. The trust boundary

There is exactly one boundary in this system, and it is **nominal**: marked, documented, and not
enforced.

```
  ┌─────────────── UNTRUSTED ────────────────┐   ┌────── NOMINALLY PRIVILEGED ──────┐
  │  POST   /carts                           │   │  POST /admin/coupons             │
  │  GET    /carts/{id}                      │   │  GET  /admin/coupons             │
  │  POST   /carts/{id}/items                │   │  GET  /admin/report              │
  │  PUT    /carts/{id}/items/{pid}          │   │                                  │
  │  DELETE /carts/{id}/items/{pid}          │   │  marked by: /admin prefix        │
  │  POST   /carts/{id}/reprice              │   │          +  X-Admin-Token header │
  │  POST   /carts/{id}/checkout             │   │  compared to a config constant   │
  │  GET    /orders/{id}                     │   │  -> 403 FORBIDDEN when absent    │
  │  GET    /products, /customers            │   └──────────────────────────────────┘
  └──────────────────────────────────────────┘
```

**How it is marked** [D28]: path prefix *and* a shared static token. Two markers, because the prefix
alone is a naming convention that no code checks, and a middleware that checks a token is a real
enforcement point that a reviewer can put a finger on — even though the secret is a constant in
config.

**What it is not.** A shared static token is not authentication: it does not identify a caller, it
cannot be revoked per-caller, it is compared without constant-time semantics, and it is in the repo.
It is a placeholder that makes the boundary *locatable in the code* rather than only in prose.

**`customer_id` is a claim, not an identity.** [D10] [D11] Any caller may assert any of the five
seeded ids. Nothing verifies it. Every abuse case below that depends on impersonation is therefore
**open by construction**, and that is a documented consequence of "auth not required", not an
oversight.

---

## 2. Abuse cases, by invariant

### A1 — Coupon farming *(against I7, I8, and the economics)*

**Attack.** Coupons cost the attacker only *order count*. With `n = 5`, place five trivial orders —
one USB-C cable at ₹399 each — to mint a 10%-off coupon, then spend it on a ₹24,999 dock. Net gain
per cycle ≈ ₹2,500 for ₹1,995 of cables. Repeat indefinitely, because a discounted order **still
counts toward the next milestone** [D16], so cycles compound rather than reset.

**Why the design permits it.** Three chosen semantics combine: no minimum order value (not in the
spec), discounted orders count [D16], and `customer_id` is unverified [D10] — so the attacker need
not even be one customer.

**What actually limits it.** Only inventory, and only accidentally.

**Mitigations, not implemented, in ascending cost:**
1. Minimum order value for milestone eligibility — a single `if` at the counter increment.
2. Exclude discounted orders from the counter — reverses [D16]; a one-line change, and precisely the
   "change one business rule" exercise the spec warns about.
3. Per-customer milestone counters instead of a global one — changes the reward from a store-wide to
   a per-customer program, which is a product decision, not a security fix.
4. Real authentication plus velocity limits — out of scope.

**Residual risk accepted.** In a system with no auth, no payment, and no customer verification,
farming is not meaningfully preventable. Documenting the cheapest real fix (#1) is the deliverable.

### A2 — Idempotency-key collision *(against I3)*

**Attack (accidental).** A naive client uses `Idempotency-Key: 1` for every checkout. Under a global
key scope this collides with **other customers'** keys and returns their orders — a cross-customer
data leak caused by nothing but a lazy client.

**Closed by design.** Scoping to `(cart_id, key)` [D19] makes collisions harmless: a collision can
only occur within one cart, and a cart is checked out at most once anyway (**I2**). The lazy client
is *safe* here, not dangerous. This was the deciding argument for cart-scoping over the more
conventional per-customer scope.

**Residual.** A caller who knows a `cart_id` **and** a key used against it can observe that order via
replay. That reduces to A4 (cart-id guessing), below.

### A3 — Idempotency-key reuse with a different payload *(against I3, I9)*

**Attack.** Capture a victim's in-flight request, replay it with `coupon_code` swapped or removed.
Under replay-and-ignore-body semantics the server would return a response that does not correspond
to the request — a *discounted* order to a caller that asked for none, or a silently dropped coupon
the caller believes was applied. Both are silent wrong-money outcomes.

**Closed by design.** The canonical body is SHA-256 fingerprinted at first claim; a mismatch returns
`409 IDEMPOTENCY_KEY_REUSED` [D20]. The check runs **before** the in-progress check, so a mismatched
replay is never mistaken for a legitimate retry.

**Residual.** Fingerprinting proves the *payloads* match; it proves nothing about *who* sent them.
Without auth, an attacker who replays a byte-identical request gets a byte-identical replay — which
is exactly correct idempotent behaviour, and exactly why it is not a defence against impersonation.

### A4 — Cart-id and order-id guessing *(against confidentiality)*

**Attack.** `GET /carts/{id}` and `GET /orders/{id}` have no ownership check whatsoever. Anyone
holding an id sees the contents; anyone who can enumerate ids sees everything.

**Partially closed.** Ids must be `uuid4`-derived opaque strings [D37] (`cart_<uuid4hex>`,
`ord_<uuid4hex>`) — never sequential, never a counter. 122 bits of entropy makes enumeration
infeasible. **This is the only defence, and it is secrecy of a capability URL, not authorization.**

**Deliberately left open.** An ownership check is impossible without an identity to check against,
and identity is out of scope. Note the asymmetry: `Order.customer_id` exists and *could* be
compared, but the comparison would be against another unverified claim, producing the appearance of
access control without the substance. **Fake access control is worse than none**, because it invites
a reader to trust it. Left open, and said plainly.

### A5 — Negative, zero, fractional, or overflowing quantities *(against I1, I9, I14)*

**Attacks.**
- `quantity: -5` → a negative line total → a negative order total, or a *credit*.
- `quantity: 0` → a zero-value line that pollutes the report's item counts.
- `quantity: 2.5` → a float enters the money path, breaking **I14** at the first multiplication.
- `quantity: 10**500` → **Python ints are arbitrary precision**, so unlike JavaScript there is no
  silent wraparound at 2⁵³ and no precision loss. Instead the request succeeds arithmetically and
  burns CPU and memory on a bignum, and the resulting JSON response is enormous. **The failure mode
  is resource exhaustion, not arithmetic corruption** — which is why a lower bound alone is
  insufficient and an explicit **upper** bound is required. [D32]

**Closed by design.** `Field(gt=0, le=100)` on every quantity, `strict=True` so `2.5` and `"5"` are
rejected rather than coerced. Enforced at the Pydantic edge, so it appears in the OpenAPI schema
rather than hiding in handler code. Money fields are never accepted from a client at all — prices
come only from seed data, so there is no client-controlled money input anywhere in the API. That is
the strongest single property in this model.

### A6 — The report as an information leak *(against confidentiality)*

**Exposure.** `GET /admin/report` aggregates the entire store: total revenue, per-product volumes,
coupon counts, and the configured `n` and `x`. Behind only a static shared token, anyone who obtains
that token — from the repo, a screenshot, a log, a URL, a CI transcript — reads the whole business.

**Partially closed.** Placed behind `/admin` + `X-Admin-Token` [D28]. The report exposes **no
per-customer data**: no customer ids, no order ids, no coupon codes. That is a deliberate choice —
aggregate-only means a token leak exposes business metrics, not customers.

`GET /admin/coupons` **does** expose live coupon codes and owner ids, so it is a materially more
sensitive endpoint than the report despite sitting behind the same token. In a real system these
would carry different scopes. Noted, not implemented.

**Deferred.** Real admin authn/authz, per-endpoint scopes, audit logging of admin actions.

### A7 — Coupon-code guessing *(against I4, I6)*

**Attack.** Guess a code, then guess its owner. The owner space is **five seeded ids** — trivially
enumerable — so if the API confirmed a code existed, the attacker would need at most five attempts
to redeem it.

**Closed by design.** `COUPON_INVALID` is returned **identically** for "no such code" and "not your
code" [D17], so there is no oracle to distinguish them and the five-owner search is worthless
without a confirmed code. Codes themselves must be `uuid4`-derived, not sequential and not derived
from the milestone index (`COUPON5` would be guessable by construction). The precise reason is
written to the server log, so debuggability is preserved without leaking it to the caller.

**Note the cost.** This makes the API *less* helpful, in tension with the spec's "errors should be
distinguishable and useful". The resolution: distinguishable **by cause the caller can act on**. A
caller can do nothing about "this coupon belongs to someone else", so telling them buys nothing and
costs an enumeration oracle.

### A8 — Reservation exhaustion / denial of inventory *(against I1 availability)*

**Attack.** Hold inventory in the `RESERVED` state to deny it to real customers.

**Closed by design, structurally.** Add-to-cart reserves nothing [D7], so a cart cannot hold stock —
which is why no cart TTL is needed. Inventory is reserved only *inside* the checkout critical
section, and the `try/except` releases on every exit path (**I13**). The reservation window is
bounded by the payment latency, and no code path can abandon one.

**Residual.** A slow or hanging payment gateway holds the product lock for its full duration,
blocking every other checkout for that product. **There is no timeout on the payment call.** That is
a real availability weakness and it is the honest answer to "identify a weakness in your
implementation": the fix is `asyncio.wait_for` around `charge()` plus a compensating release, and it
is deferred only because a fake gateway never hangs.

### A9 — Concurrent coupon redemption *(against I4)*

**Attack.** Fire N concurrent checkouts with the same coupon code, hoping two commit.

**Closed by design.** Reservation happens under the coupon-ledger lock, and `reserve_locked` is
**synchronous** — it cannot yield, so the check-and-set is atomic. The first caller moves the coupon
to `RESERVED`; every other caller arrives after the winner has committed and gets `422 COUPON_ALREADY_REDEEMED` —
`RESERVED` is never observable to a competitor, because the ledger lock is held across payment [D39]. Exercised directly
by test C5.

### A10 — Admin milestone double-reward *(against I7)*

**Attack.** Fire N concurrent `POST /admin/coupons`, hoping two reward the same milestone.

**Closed by design.** `generate()` acquires the coupon-ledger lock, and the
read-`last_rewarded_milestone`/write sequence inside it is synchronous. One winner per milestone.
**This is the first invariant to break under multiple instances** (TAD §9), because two processes
have two independent `last_rewarded_milestone` values.

---

## 3. Input validation, per endpoint

Enforced by Pydantic v2 at the edge with `strict=True` [D36]; violations become
`422 VALIDATION_FAILED` after normalisation into the house envelope.

| Endpoint | Field | Rule | On violation |
|---|---|---|---|
| `POST /carts` | — | body must be empty or absent | 422 |
| `POST /carts/{id}/items` | `product_id` | non-empty str, ≤ 64 chars, must exist | 422 / `404 PRODUCT_NOT_FOUND` |
| | `quantity` | `int`, strict, `gt=0`, `le=100` | 422 |
| | | ≤ current available stock (**advisory**) | `422 QUANTITY_EXCEEDS_STOCK` |
| `PUT /carts/{id}/items/{pid}` | `quantity` | `int`, strict, `gt=0`, `le=100` | 422 |
| `DELETE /carts/{id}/items/{pid}` | path only | idempotent — a missing line is a no-op | 204 either way |
| `POST /carts/{id}/reprice` | — | cart must be `OPEN` | `409 CART_ALREADY_CHECKED_OUT` |
| `POST /carts/{id}/checkout` | `Idempotency-Key` | header **required**, non-empty, ≤ 128 chars. No minimum: lazy keys are safe under cart scoping [D19] | `400 IDEMPOTENCY_KEY_REQUIRED` / 422 |
| | `customer_id` | non-empty str, must be a seeded id | `422 UNKNOWN_CUSTOMER` |
| | `coupon_code` | optional str, ≤ 64 chars | 422 |
| | body | must have no extra fields (`extra="forbid"`) | 422 |
| `GET /orders/{id}` | path | opaque str, ≤ 128 chars | 404 |
| `POST /admin/coupons` | `X-Admin-Token` | must equal config | `403 FORBIDDEN` |
| `GET /admin/report` | `X-Admin-Token` | must equal config | `403 FORBIDDEN` |
| `GET /admin/coupons` | `X-Admin-Token` | must equal config | `403 FORBIDDEN` |

**Cross-cutting rules.**
- `extra="forbid"` on **every** request model. Silently ignoring unknown fields hides client bugs and
  is how a `coupon_code` typo becomes an undiscounted order the customer expected to be discounted.
- **No client-supplied money, anywhere.** Prices come only from seed data. `x` comes only from
  config. There is no endpoint through which a caller can influence a money value except by choosing
  quantities within `[1, 100]`.
- Ids in responses are `uuid4`-derived and opaque. No sequential ids for carts, orders, or coupons
  (`Order.sequence` is internal to milestone attribution and is not an addressable id).
- No stack traces, no internal type names, no file paths in any error body. `INTERNAL_ERROR` is
  opaque.

---

## 4. Deliberately deferred — and what it would take to close

| Deferred | Why | Cost to close |
|---|---|---|
| Authentication | Out of scope per spec | Session or JWT issuance; ~2–3h with a real store |
| Ownership checks on `GET /carts`, `GET /orders` | Needs a verified identity; checking an unverified claim is fake access control (A4) | Trivial *after* authn — one comparison per handler |
| Real admin authz | Static shared token instead (A6) | Scoped roles + per-endpoint checks; ~1h after authn |
| Constant-time token comparison | Irrelevant while the token is a repo constant | `secrets.compare_digest`, one line — do it if the token ever becomes real |
| Rate limiting / velocity limits | Does not fit the timebox; would blunt A1 and A7 | Middleware + a counter store; ~1h in-process, more distributed |
| Payment call timeout (A8) | Fake gateway never hangs | `asyncio.wait_for` + compensating release; ~20 min. **The highest value/effort ratio on this list** |
| Minimum order value for milestone eligibility (A1) | Not in the spec; a product decision | One `if`; ~10 min |
| Audit log of admin actions | No log infrastructure in scope | Append-only list + a read endpoint; ~30 min |
| Idempotency record TTL / eviction | Unbounded growth is irrelevant at demo scale; it *is* a leak | Timestamp + periodic sweep; ~30 min. Mandatory before production |
| Request size limits, body caps | Server default only; A5's bignum case is closed by `le=100` | ASGI middleware; ~15 min |
| TLS, secrets management, CORS policy | Deployment concerns, no deployment in scope | — |

---

## 5. Summary — what this model actually claims

**Closed by design and tested:** oversell (A5 quantities, A8 reservations), concurrent coupon
redemption (A9), concurrent milestone reward (A10), idempotency-key collision (A2) and payload reuse
(A3), coupon enumeration (A7).

**Open by construction, and stated as such:** everything that depends on knowing *who is calling* —
cart and order confidentiality (A4), coupon farming (A1), the blast radius of an admin-token leak
(A6). These are open because identity is out of scope, not because they were missed.

**The one weakness worth fixing first:** the unbounded payment call inside the critical section (A8).
It is the only issue here that can degrade the service without any attacker at all.
