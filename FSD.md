# FSD — Frontend Specification

> ## ⚠️ THIS ENTIRE DOCUMENT IS OPTIONAL AND CUTTABLE
>
> The spec says frontend work "will not compensate for an unreliable backend." Build this **only**
> after every ticket in `FTL.md` §Critical Path is green. If the timebox is threatened, delete
> `web/` and this document, and say so in `DECISIONS.md`. Nothing else depends on it. [D34]

---

## 1. What this is, and what it is not

**It is a demo harness.** Its only job is to make backend behaviour that is otherwise invisible —
concurrency, idempotency, coupon races — *visible in a browser in one click*.

**It is not a shopping UI.** No product browsing, no cart-building flow, no checkout funnel, no
responsive design, no framework, no build step, no design system, no accessibility budget, no
styling beyond what makes numbers legible.

If a reviewer opens it and thinks "that's a nice store", the scope was wrong. They should think
"oh — 10 requests, 1 order, and I can see it."

## 2. Constraints

- **One file:** `web/index.html`. Inline `<style>` and `<script>`. No build, no npm, no bundler.
- **No dependencies.** `fetch` and vanilla DOM only. No React, no Tailwind, no CDN.
- **Served by FastAPI** via `StaticFiles` at `/demo`, so there is no second server and no CORS.
- Owned by **Session C**. Sessions A and B never touch it.
- Budget: **60 minutes, hard.** Over that, cut it.

## 3. Layout

A single scrolling page, four panels stacked, monospace, plain table borders.

```
┌──────────────────────────────────────────────────────────────────────┐
│  SETUP     [admin token: ______]  [reset seed data]   n=5  x=10      │
├──────────────────────────────────────────────────────────────────────┤
│  PANEL 1 — CONCURRENT CHECKOUT / OVERSELL                            │
│  PANEL 2 — IDEMPOTENCY STORM                                         │
│  PANEL 3 — COUPON RACE                                               │
│  PANEL 4 — LIVE REPORT                                               │
└──────────────────────────────────────────────────────────────────────┘
```

## 4. Panel 1 — Concurrent checkout against limited stock

**Demonstrates I1.** Ties to test C3.

**Controls:** product selector (default `prd_headset`, stock 1), `N` concurrent checkouts
(default 10), **[FIRE]**.

**Behaviour.** Creates N carts, adds 1 unit of the product to each, then fires N checkouts in a
single `Promise.all` — each with its own fresh `Idempotency-Key`, so idempotency is *not* what is
being tested here.

**Output.**

```
FIRED 10 concurrent checkouts against prd_headset (stock 1)

  201 Created                      1     ← at most stock, always
  409 INSUFFICIENT_INVENTORY       9

  stock before  1      sold  1      available  0      reserved  0
  INVARIANT I1  stock_total == available + reserved + sold   ✅
```

The success count **must never exceed the starting stock**, and the invariant line must be computed
from `/admin/report` + `/products`, not asserted in JavaScript.

## 5. Panel 2 — Idempotency storm

**Demonstrates I3.** Ties to test C4.

**Controls:** `N` repeats (default 10), **[FIRE SAME KEY N TIMES]**.

**Behaviour.** Creates **one** cart, generates **one** `Idempotency-Key`, fires N concurrent
checkouts all using it.

**Output.**

```
FIRED 10 concurrent checkouts, ONE Idempotency-Key: 3f9a2c...

  201 Created                      1
  409 REQUEST_IN_PROGRESS          9     ← concurrent replay, correctly refused
  distinct order ids               1     ← the number that matters

  [REPLAY THE SAME KEY, SEQUENTIALLY]
  200 OK   Idempotent-Replay: true      order ord_4b1e...  (same id) ✅
```

The **sequential replay button is the point of this panel**: it shows the *other* half of the
contract — `409` while in flight, `200` + identical body afterwards. One without the other does not
demonstrate the design.

Add a third button, **[REPLAY WITH DIFFERENT BODY]**, which re-sends the same key with the coupon
field flipped and shows `409 IDEMPOTENCY_KEY_REUSED`. That is three distinct responses from one key,
which is the whole idempotency model on one screen.

## 6. Panel 3 — Coupon race

**Demonstrates I4, I5, I6.** Ties to tests C5 and C6.

**Controls:** **[SETUP: place n orders]**, **[GENERATE COUPON]**, `N` racers (default 5),
**[RACE FOR COUPON]**, and a **[FORCE PAYMENT FAILURE]** toggle.

**Behaviour.**
1. Setup places `n` cheap orders so a milestone is reached. Shows the running count toward `n`.
2. Generate calls `POST /admin/coupons` and displays the code, percent, milestone, and **owner**.
3. Race creates N carts *for the owning customer* and fires N concurrent checkouts all presenting
   that one code.
4. The failure toggle points the server at `AlwaysDeclineGateway` for one request, to show the
   coupon returning to `AVAILABLE`.

**Output.**

```
COUPON  CPN-7f3a12  10%  milestone 1  owner cus_2

RACE — 5 concurrent checkouts, same coupon
  201 Created                 1
  409 COUPON_IN_USE           4
  coupon state now: REDEEMED by ord_9c2f...        ✅ I4

WRONG OWNER — cus_4 presents cus_2's coupon
  422 COUPON_INVALID   (identical to a code that does not exist)  ✅ I6

PAYMENT FAILURE — checkout reserves the coupon then declines
  402 PAYMENT_DECLINED
  coupon state now: AVAILABLE   (returned, not lost)              ✅ I5
  reserved inventory: 0         (released)                        ✅ I13
```

The payment-failure row is the most valuable output in the whole harness — **I5** is the one
invariant the spec calls out by name that has no visible symptom otherwise.

## 7. Panel 4 — Live report

**Demonstrates I11, I12, I15.**

Polls `GET /admin/report` every 2s (toggleable; default **off** so it does not interleave with the
race panels and confuse their output).

**Output.**

```
orders placed        12
items purchased      prd_cable  15    prd_headset  1    prd_dock  2

gross      ₹1,04,970.00
discount   ₹    4,499.00
net        ₹1,00,471.00        I12  gross - discount == net   ✅

coupons    generated 2   available 1   reserved 0   redeemed 1
                          I15  generated == avail + reserved + redeemed   ✅
```

Both reconciliation lines are computed **in the browser from the response body** and shown with a
pass/fail marker. A reviewer who trusts nothing else can watch these two identities hold while the
race panels fire.

Money is rendered by dividing minor units **for display only** — never for arithmetic. All maths in
the page is on integers. [D1]

## 8. Acceptance criteria

- **F1** Opens at `/demo` with the API already running. No build step, no second server.
- **F2** Every panel shows the **raw status-code histogram**, not a prettified summary. The codes are
  the evidence.
- **F3** Every invariant marker is computed from an API response, never hardcoded.
- **F4** Panels are independently runnable in any order and never require a server restart.
- **F5** A failed request renders its `error.code` verbatim. No `catch {}` that swallows anything.
- **F6** Total file size under ~400 lines. If it grows past that, the scope drifted into a shopping UI.

## 9. Explicit non-goals

Product browsing, add/remove cart UI, quantity steppers, a customer picker beyond a raw text input,
routing, state management, loading skeletons, animation, error toasts, mobile layout, dark mode,
i18n, accessibility affordances, and any styling that is not a table border.

## 10. Cut order

If time runs short, cut in this order — each line is independently droppable:

1. Panel 4 auto-poll (keep a manual **[REFRESH]** button)
2. Panel 3 wrong-owner row
3. Panel 3 entirely
4. Panel 1 entirely (test C3 already proves it; the panel only makes it visible)
5. **The whole frontend.** Keep Panel 2 longest — the idempotency contract is the least obvious
   behaviour in the system and the hardest to appreciate from a test file alone.
