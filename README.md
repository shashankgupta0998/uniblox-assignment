# Checkout and Rewards Service

A small e-commerce backend: carts, an idempotent checkout, milestone reward coupons ("every n-th
placed order earns one x% coupon"), and a reconciling admin report. It is written to hold fifteen
named invariants under concurrent load, and the test suite is built around proving them. The
reasoning behind every design choice lives in [`DECISIONS.md`](DECISIONS.md); this file is how to
run it and what it exposes.

Stack: Python 3.11+, FastAPI, Pydantic v2, pytest. In-memory state, no database, no auth beyond a
static admin token, a fake payment gateway. Those are deliberate non-goals, not gaps.

## Run it

Four commands from clone to a running server:

```bash
git clone <this repo> uniblox && cd uniblox          # 1
python3 -m venv .venv                                 # 2
.venv/bin/pip install -e ".[dev]"                     # 3
.venv/bin/uvicorn src.main:app                        # 4  -> http://127.0.0.1:8000
```

Then open **http://127.0.0.1:8000/docs** — the generated OpenAPI document (Swagger UI) is the API
reference. `/openapi.json` is the raw schema.

Run the tests:

```bash
.venv/bin/pytest -q
```

Every test names the invariant it protects in its docstring. The suite is fully asynchronous
(`asyncio_mode = "auto"`) and drives the app in-process; nothing needs to be running first.

## Configuration

All from environment variables, all with defaults (`src/config.py`):

| Variable | Default | Meaning |
|---|---|---|
| `REWARD_N` | `5` | Every `n`-th placed order reaches a milestone |
| `REWARD_X` | `10` | Discount percent of the milestone coupon |
| `ADMIN_TOKEN` | `dev-admin-token` | Value of the `X-Admin-Token` header for `/admin/*` |
| `PAYMENT_LATENCY_SECONDS` | `0.0` | Simulated gateway latency. The fake gateway always yields to the event loop, even at 0, so the concurrency tests have a real in-flight window |

Example: `REWARD_N=3 REWARD_X=25 .venv/bin/uvicorn src.main:app`.

Seed data: six products (`prd_keyboard`, `prd_mouse`, `prd_monitor`, `prd_cable`, `prd_dock` with
stock 3, `prd_headset` with stock 1) and five customers (`cus_1` … `cus_5`). All money is integer
minor units (paise); every money field ends in `_minor`. Ids are opaque and uuid4-derived.

## The API, as a flow

Every example below was run against a live server and the output pasted (trimmed). `B` is
`http://127.0.0.1:8000`. Money is in paise: `39900` is ₹399.00.

### Catalogue

```bash
curl $B/products
# 200
# [{"id":"prd_cable","name":"USB-C Cable","unit_price_minor":39900,"stock_total":100,"available":100,"reserved":0,"sold":0}, ...]

curl $B/customers
# 200  [{"id":"cus_1","name":"Asha Rao"}, ..., {"id":"cus_5","name":"Esha Singh"}]
```

`stock_total == available + reserved + sold` on every product, at every moment (**I1**).

### Carts

```bash
curl -X POST $B/carts
# 201  {"id":"crt_007765…","state":"OPEN","lines":[],"gross_minor":0,"has_price_changes":false}

CART=crt_007765433f124b65ab5bb5395206fb7b

curl -X POST $B/carts/$CART/items -H 'Content-Type: application/json' \
     -d '{"product_id":"prd_dock","quantity":1}'
# 201  {"id":"crt_007765…","state":"OPEN","lines":[{"product_id":"prd_dock","product_name":"Thunderbolt Dock",
#       "quantity":1,"unit_price_minor":2499900,"line_total_minor":2499900,"current_unit_price_minor":2499900,
#       "price_changed":false}],"gross_minor":2499900,"has_price_changes":false}

curl -X POST $B/carts/$CART/items -H 'Content-Type: application/json' \
     -d '{"product_id":"prd_cable","quantity":2}'
# 201  ... two lines, "gross_minor":2579700

curl -X PUT $B/carts/$CART/items/prd_cable -H 'Content-Type: application/json' -d '{"quantity":3}'
# 200  ... cable line now "quantity":3,"line_total_minor":119700 ; "gross_minor":2619600

curl -i -X DELETE $B/carts/$CART/items/prd_dock
# HTTP/1.1 204 No Content

curl $B/carts/$CART
# 200  {"id":"crt_007765…","state":"OPEN","lines":[{"product_id":"prd_cable",...,"quantity":3,
#       "unit_price_minor":39900,"current_unit_price_minor":39900,"price_changed":false}],
#       "gross_minor":119700,"has_price_changes":false}

curl -X POST $B/carts/$CART/reprice
# 200  same cart, every line re-snapshotted to its current price (see PRICE_CHANGED below)
```

The unit price is **snapshotted** into the line when it is added or its quantity is set. Adding a
product already in the cart increments its quantity. Adding above current stock is refused with
`422 QUANTITY_EXCEEDS_STOCK`, but adding reserves nothing: ten carts may each hold the last unit,
and exactly one wins at checkout.

### Checkout

The `Idempotency-Key` header is required. The key is scoped to the cart.

```bash
curl -X POST $B/carts/$CART/checkout -H 'Content-Type: application/json' \
     -H 'Idempotency-Key: 7d1c0e2a' -d '{"customer_id":"cus_1","coupon_code":null}'
# 201  {"id":"ord_2d0320…","sequence":1,"cart_id":"crt_007765…","customer_id":"cus_1",
#       "lines":[{"product_id":"prd_cable","product_name":"USB-C Cable","quantity":3,
#                 "unit_price_minor":39900,"line_total_minor":119700}],
#       "gross_minor":119700,"discount_minor":0,"net_minor":119700,
#       "coupon_code":null,"discount_percent":null,"state":"PLACED"}
```

Replay the same request with the same key: `200`, the identical body, and a header saying so.

```bash
curl -i -X POST $B/carts/$CART/checkout -H 'Content-Type: application/json' \
     -H 'Idempotency-Key: 7d1c0e2a' -d '{"customer_id":"cus_1","coupon_code":null}'
# HTTP/1.1 200 OK
# idempotent-replay: true
# {"id":"ord_2d0320…", ... identical ...}
```

Same key, different body — refused, never silently replayed:

```bash
curl -X POST $B/carts/$CART/checkout -H 'Content-Type: application/json' \
     -H 'Idempotency-Key: 7d1c0e2a' -d '{"customer_id":"cus_1","coupon_code":"nope"}'
# 409  {"error":{"code":"IDEMPOTENCY_KEY_REUSED","message":"This Idempotency-Key was already used with a different request body.","details":{"cart_id":"crt_007765…"}}}
```

A cart is checked out at most once, whatever key you use (**I2**):

```bash
curl -X POST $B/carts/$CART/checkout -H 'Content-Type: application/json' \
     -H 'Idempotency-Key: other' -d '{"customer_id":"cus_1","coupon_code":null}'
# 409  {"error":{"code":"CART_ALREADY_CHECKED_OUT","message":"This cart has already been checked out.","details":{"cart_id":"crt_007765…"}}}
```

A replay that arrives while the original is still in flight gets `409 REQUEST_IN_PROGRESS`; back
off and retry, and you receive the `200` replay above. A checkout that **fails** releases the key,
so retrying with the same key is a genuine new attempt, not a cached failure.

### Orders

```bash
curl $B/orders/ord_2d0320eef33844da86135549c7a460ec
# 200  the order exactly as placed: its own price snapshot, never recomputed (I10)

curl $B/orders/ord_nope
# 404  {"error":{"code":"ORDER_NOT_FOUND","message":"No such order.","details":{"order_id":"ord_nope"}}}
```

### Coupons (administrative)

Every `n`-th **placed** order reaches a milestone; each milestone is rewarded exactly once with one
coupon, bound to the customer who placed order number `k·n`. With one order placed and `n = 5`:

```bash
curl -X POST $B/admin/coupons -H 'X-Admin-Token: dev-admin-token'
# 409  {"error":{"code":"NO_ELIGIBLE_MILESTONE","message":"No milestone has been reached that is not already rewarded.","details":{"placed_orders":1,"next_milestone_at":5}}}
```

After four more orders (the fifth placed by `cus_5`):

```bash
curl -X POST $B/admin/coupons -H 'X-Admin-Token: dev-admin-token'
# 201  {"coupon":{"code":"32eca5538cf84bd0","percent":10,"milestone":1,"owner_customer_id":"cus_5",
#       "state":"AVAILABLE","redeemed_by_order_id":null},"pending_milestones":0}

curl $B/admin/coupons -H 'X-Admin-Token: dev-admin-token'
# 200  [{"code":"32eca5538cf84bd0","percent":10,"milestone":1,"owner_customer_id":"cus_5","state":"AVAILABLE","redeemed_by_order_id":null}]
```

`pending_milestones` says whether to call again: one coupon per call, lowest unrewarded milestone
first. Presenting the coupon at checkout, as its owner, on a cart holding one monitor:

```bash
curl -X POST $B/carts/$CART2/checkout -H 'Content-Type: application/json' \
     -H 'Idempotency-Key: c3' -d '{"customer_id":"cus_5","coupon_code":"32eca5538cf84bd0"}'
# 201  {"id":"ord_e46c76…","sequence":6,...,"gross_minor":1899900,"discount_minor":189990,"net_minor":1709910,
#       "coupon_code":"32eca5538cf84bd0","discount_percent":10,"state":"PLACED"}
```

The discount is computed once per order, in `Decimal`, rounded half-even, clamped to gross, and
`gross - discount == net` exactly (**I9**, **I12**). Presented by anyone else, or with a code that does
not exist, the response is byte-identical, so a code's existence is never revealed (**I6**):

```bash
curl -X POST $B/carts/$CART2/checkout -H 'Content-Type: application/json' \
     -H 'Idempotency-Key: c1' -d '{"customer_id":"cus_1","coupon_code":"32eca5538cf84bd0"}'
# 422  {"error":{"code":"COUPON_INVALID","message":"This coupon cannot be applied.","details":{}}}

curl -X POST $B/carts/$CART2/checkout -H 'Content-Type: application/json' \
     -H 'Idempotency-Key: c2' -d '{"customer_id":"cus_1","coupon_code":"nope"}'
# 422  {"error":{"code":"COUPON_INVALID","message":"This coupon cannot be applied.","details":{}}}
```

A coupon is redeemed at most once (**I4**). A checkout that fails after reserving it returns it to
`AVAILABLE`; it is never lost (**I5**).

### Report (administrative)

```bash
curl $B/admin/report -H 'X-Admin-Token: dev-admin-token'
# 200  {"orders_placed":6,
#       "items_purchased":[{"product_id":"prd_monitor","name":"27\" Monitor","quantity":1},
#                          {"product_id":"prd_cable","name":"USB-C Cable","quantity":7}],
#       "gross_minor":2179200,"discount_minor":189990,"net_minor":1989210,
#       "coupons_generated":1,"coupons_available":0,"coupons_reserved":0,"coupons_redeemed":1,
#       "n":5,"x":10}
```

A pure read: it takes no locks and mutates nothing, so two calls return identical bodies (**I11**).
It reconciles: `gross - discount == net` (**I12**) and
`generated == available + reserved + redeemed` (**I15**). `reserved` is exposed precisely so that
identity holds while a checkout is mid-flight.

## Errors

Every failure, from any layer, has one shape and a stable machine-readable `code`. One code maps to
exactly one HTTP status. FastAPI's default validation body never appears.

```json
{"error": {"code": "<STABLE_CODE>", "message": "...", "details": {...}}}
```

Observed cases:

```bash
# missing Idempotency-Key
curl -X POST $B/carts/$CART/checkout -H 'Content-Type: application/json' -d '{"customer_id":"cus_1","coupon_code":null}'
# 400  {"error":{"code":"IDEMPOTENCY_KEY_REQUIRED","message":"The Idempotency-Key header is required on checkout.","details":{}}}

# validation: quantity must be an integer in [1, 100]; unknown fields are rejected; no money field is ever accepted
curl -X POST $B/carts/$CART/items -H 'Content-Type: application/json' -d '{"product_id":"prd_cable","quantity":0}'
# 422  {"error":{"code":"VALIDATION_FAILED","message":"Request validation failed.","details":{"errors":[{"loc":["body","quantity"],"message":"Input should be greater than 0"}]}}}
curl -X POST $B/carts/$CART/items -H 'Content-Type: application/json' -d '{"product_id":"prd_cable","quantity":1,"unit_price_minor":1}'
# 422  {"error":{"code":"VALIDATION_FAILED",...,"details":{"errors":[{"loc":["body","unit_price_minor"],"message":"Extra inputs are not permitted"}]}}}

# add-time stock check (advisory; reserves nothing)
curl -X POST $B/carts/$C2/items -H 'Content-Type: application/json' -d '{"product_id":"prd_headset","quantity":2}'
# 422  {"error":{"code":"QUANTITY_EXCEEDS_STOCK","message":"Requested quantity exceeds current stock.","details":{"product_id":"prd_headset","requested":2,"available":1}}}

# checkout-time contention: two carts each held the last headset; the second to check out loses
curl -X POST $B/carts/$H2/checkout -H 'Content-Type: application/json' -H 'Idempotency-Key: h2' -d '{"customer_id":"cus_3","coupon_code":null}'
# 409  {"error":{"code":"INSUFFICIENT_INVENTORY","message":"Not enough stock to reserve.","details":{"product_id":"prd_headset","requested":1,"available":0}}}

# price drift: a catalogue price changed after the line was added
curl -X POST $B/carts/$C3/checkout -H 'Content-Type: application/json' -H 'Idempotency-Key: p1' -d '{"customer_id":"cus_1","coupon_code":null}'
# 409  {"error":{"code":"PRICE_CHANGED","message":"One or more prices changed since the items were added; reprice the cart to accept.",
#       "details":{"changed":[{"product_id":"prd_mouse","old_unit_price_minor":129900,"new_unit_price_minor":139900}]}}}
#      GET /carts/$C3 shows "price_changed":true on the line; POST /carts/$C3/reprice accepts the new price;
#      the same checkout, same key, then succeeds: 201 at the new price. Nothing was reserved or consumed by the refusal.

# empty cart
# 422  {"error":{"code":"CART_EMPTY","message":"The cart has no items.","details":{"cart_id":"crt_92675d…"}}}

# unknown ids
curl $B/carts/crt_nope           # 404  {"error":{"code":"CART_NOT_FOUND",...,"details":{"cart_id":"crt_nope"}}}
curl -X POST $B/carts/$C2/items -H 'Content-Type: application/json' -d '{"product_id":"prd_nope","quantity":1}'
                                 # 404  {"error":{"code":"PRODUCT_NOT_FOUND",...,"details":{"product_id":"prd_nope"}}}

# missing or wrong admin token
curl $B/admin/report
# 403  {"error":{"code":"FORBIDDEN","message":"A valid X-Admin-Token header is required.","details":{}}}
```

The full catalogue of codes and statuses is in `TAD.md` §7 and, live, in `/docs`.

## Administrative operations

These require the `X-Admin-Token` header (value from `ADMIN_TOKEN`, default `dev-admin-token`).
Missing, empty, or wrong → `403 FORBIDDEN`.

| Operation | Route |
|---|---|
| Generate the next milestone coupon | `POST /admin/coupons` |
| List all coupons and their states | `GET /admin/coupons` |
| The reconciling report | `GET /admin/report` |

Everything else is a customer operation. `customer_id` is a claim, not a verified identity; there
is no authentication by design (see `DECISIONS.md`).

## Invariants and where they are tested

| # | Invariant | Tests |
|---|---|---|
| I1 | `stock_total == available + reserved + sold`, never oversell | `tests/test_concurrency_inventory.py`, `tests/smoke/test_a3.py` |
| I2 | A cart is checked out at most once | `tests/test_validation.py`, `tests/smoke/test_a5.py` |
| I3 | One `(cart_id, Idempotency-Key)` yields at most one order under any interleaving | `tests/test_idempotency.py`, `tests/smoke/test_a4.py` |
| I4 | A coupon is redeemed at most once, by one order | `tests/test_coupons_concurrency.py`, `tests/smoke/test_a6.py` |
| I5 | A failed checkout returns the coupon to `AVAILABLE` | `tests/test_release_on_failure.py` |
| I6 | A coupon is redeemable only by its owner; wrong owner and unknown code are indistinguishable | `tests/test_coupons_concurrency.py`, `tests/smoke/test_a6.py` |
| I7 | At most one coupon per milestone, ever | `tests/test_coupons_concurrency.py`, `tests/smoke/test_a6.py` |
| I8 | Only `PLACED` orders count | `tests/test_report.py`, `tests/smoke/test_a3.py` |
| I9 | `0 <= discount <= gross` | `tests/test_money.py` |
| I10 | Orders are immutable and carry their own price snapshot | `tests/test_price_drift.py`, `tests/smoke/test_a3.py` |
| I11 | The report is a pure read | `tests/test_report.py`, `tests/test_routers_admin.py` |
| I12 | `gross - discount == net` exactly | `tests/test_money.py`, `tests/test_report.py` |
| I13 | Inventory reserved by a failed checkout is released in full | `tests/test_release_on_failure.py` |
| I14 | Money is integer minor units; no float anywhere | `tests/test_money.py`, `tests/test_schemas.py` |
| I15 | `coupons.generated == available + reserved + redeemed` | `tests/test_report.py`, `tests/smoke/test_a6.py` |

The error contract, the frozen interfaces, and the app wiring have their own tests
(`tests/test_error_envelope.py`, `tests/test_signatures.py`, `tests/test_app_factory.py`, the
`tests/test_routers_*.py` files).

## Known limitations

Deliberate, and each is reasoned through in [`DECISIONS.md`](DECISIONS.md):

- **Single process, in memory.** State lives for the process lifetime and is lost on restart. The
  concurrency guarantees rest on one event loop; `DECISIONS.md` §10 maps every in-process construct
  to its database equivalent and says what breaks first with two instances.
- **No authentication.** `customer_id` is an unverified claim; `GET /carts/{id}` and
  `GET /orders/{id}` rely on id entropy alone. The admin token is a static shared secret.
- **The threadpool caveat.** The locks are `asyncio.Lock`, which protect `async def` handlers on the
  event loop and nothing else. Every state-touching handler is `async def` for that reason; a plain
  `def` handler would run in a threadpool where the invariants do not hold.
- **Framework-level 404/405** (an unknown path, a wrong method) keep Starlette's default body. The
  error catalogue has no code for them, and inventing one was ruled out.
- **No timeout on the payment call**, no idempotency-record expiry, no coupon expiry, no rate
  limiting. See the deferred table in `DECISIONS.md` §9.

## Time spent

Approximately **__ hours wall-clock**, parallelised across three sessions (orchestrator, worker,
critic); see `DECISIONS.md` §13 for the accounting and what was cut.
