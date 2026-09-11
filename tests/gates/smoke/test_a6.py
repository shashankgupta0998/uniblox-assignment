"""A6 smoke gate — CouponService.  Protects: I4 I5 I6 I7 I15 · [D12] [D14] [D17] [D18] [D30] [D37].

Directly against src/core/coupons.py with a real store and LockManager; orders are appended with
store.append_order_locked (PLACED, chosen customers) so milestone binding is unambiguous.
  - generate rewards k = last_rewarded + 1 iff placed >= k*n, binds to orders[k*n - 1].customer_id,
    returns pending_milestones, raises NoEligibleMilestone otherwise; codes uuid4-derived.
  - reserve_locked: unknown code and wrong owner raise BYTE-IDENTICAL CouponInvalid.
  - the three *_locked methods are plain def; generate takes only the coupon-ledger lock.
  - I15 generated == available + reserved + redeemed after every transition.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import re
from typing import Any

import httpx
import pytest

from src.config import Config
from src.core.coupons import CouponService, GenerationResult
from src.core.errors import (
    CouponAlreadyRedeemed,
    CouponInUse,
    CouponInvalid,
    DomainError,
    NoEligibleMilestone,
)
from src.core.locks import LockManager
from src.core.models import Coupon, CouponState, Order, OrderLine
from src.core.store import InMemoryStore

T = 0.3
# customer of order i (1-based) — chosen so orders[4] (5th) and orders[9] (10th) differ and are unique-ish
CUSTOMERS = ["cus_1", "cus_2", "cus_3", "cus_4", "cus_2", "cus_3", "cus_1", "cus_5", "cus_4", "cus_1", "cus_5", "cus_3"]


def _append_orders(store: InMemoryStore, count: int) -> None:
    start = len(store.orders)
    for i in range(start, start + count):
        seq = i + 1
        line = OrderLine("prd_cable", "USB-C Cable", 1, 39_900, 39_900)
        store.append_order_locked(Order(
            id=f"ord_{seq}", sequence=seq, cart_id=f"crt_{seq}", customer_id=CUSTOMERS[i % len(CUSTOMERS)],
            lines=(line,), gross_minor=39_900, discount_minor=0, net_minor=39_900,
            coupon_code=None, discount_percent=None, state="PLACED",
        ))


def _i15(store: InMemoryStore) -> None:
    by_state = {s: 0 for s in CouponState}
    for c in store.coupons.values():
        by_state[c.state] += 1
    assert len(store.coupons) == by_state[CouponState.AVAILABLE] + by_state[CouponState.RESERVED] + by_state[CouponState.REDEEMED]


@pytest.fixture
def locks(fresh_store: InMemoryStore) -> LockManager:
    return LockManager(fresh_store.products.keys())


@pytest.fixture
def svc(fresh_store: InMemoryStore, locks: LockManager, config: Config) -> CouponService:
    return CouponService(fresh_store, locks, config)


# =========================================================================== shape


def test_locked_methods_are_plain_def_and_generate_is_async() -> None:
    for name in ("reserve_locked", "commit_locked", "release_locked"):
        assert not inspect.iscoroutinefunction(getattr(CouponService, name)), name
    assert inspect.iscoroutinefunction(CouponService.generate)
    assert inspect.iscoroutinefunction(CouponService.list_coupons)


# =========================================================================== generate


async def test_generate_below_n_raises_and_creates_nothing(svc: CouponService, fresh_store: InMemoryStore) -> None:
    """[D14] I7: placed < n -> NoEligibleMilestone; no coupon, milestone counter unchanged."""
    _append_orders(fresh_store, 4)  # n == 5
    with pytest.raises(NoEligibleMilestone) as exc:
        await svc.generate()
    assert exc.value.http_status == 409 and isinstance(exc.value.details, dict)
    assert fresh_store.coupons == {} and fresh_store.last_rewarded_milestone == 0
    assert await svc.list_coupons() == ()
    _i15(fresh_store)


async def test_generate_rewards_milestone_one_bound_to_nth_order(svc: CouponService, fresh_store: InMemoryStore, config: Config) -> None:
    """A6 DoD / [D12]: k=1 iff placed >= n; bound to orders[n-1].customer_id; percent == x; AVAILABLE."""
    _append_orders(fresh_store, 5)
    result = await svc.generate()
    assert isinstance(result, GenerationResult)
    c = result.coupon
    assert c.milestone == 1
    assert c.owner_customer_id == fresh_store.orders[config.n - 1].customer_id == "cus_2"
    assert c.percent == config.x == 10
    assert c.state is CouponState.AVAILABLE and c.redeemed_by_order_id is None
    assert result.pending_milestones == 0
    assert fresh_store.coupons[c.code] is c
    assert fresh_store.last_rewarded_milestone == 1
    assert await svc.list_coupons() == (c,)
    _i15(fresh_store)


async def test_generate_uses_config_n_and_x(fresh_store: InMemoryStore, locks: LockManager) -> None:
    """[D35] n and x come from Config, not constants."""
    svc = CouponService(fresh_store, locks, Config(n=2, x=25))
    _append_orders(fresh_store, 2)
    r = await svc.generate()
    assert r.coupon.milestone == 1 and r.coupon.percent == 25
    assert r.coupon.owner_customer_id == fresh_store.orders[1].customer_id == "cus_2"


async def test_pending_milestones_and_sequential_generation(svc: CouponService, fresh_store: InMemoryStore) -> None:
    """[D14]: with 2n placed, generate -> k=1 (pending 1), generate -> k=2 (pending 0), third raises."""
    _append_orders(fresh_store, 10)
    r1 = await svc.generate()
    assert r1.coupon.milestone == 1 and r1.pending_milestones == 1
    assert r1.coupon.owner_customer_id == fresh_store.orders[4].customer_id == "cus_2"
    r2 = await svc.generate()
    assert r2.coupon.milestone == 2 and r2.pending_milestones == 0
    assert r2.coupon.owner_customer_id == fresh_store.orders[9].customer_id == "cus_1"
    with pytest.raises(NoEligibleMilestone):
        await svc.generate()
    assert sorted(c.milestone for c in fresh_store.coupons.values()) == [1, 2]
    assert fresh_store.last_rewarded_milestone == 2
    _i15(fresh_store)
    _append_orders(fresh_store, 5)  # 15 placed -> milestone 3 due
    r3 = await svc.generate()
    assert r3.coupon.milestone == 3 and r3.pending_milestones == 0
    assert r3.coupon.owner_customer_id == fresh_store.orders[14].customer_id


async def test_codes_are_uuid4_derived_not_milestone_derived(svc: CouponService, fresh_store: InMemoryStore, locks: LockManager) -> None:
    """[D37]: codes are opaque — distinct across calls and across stores at the SAME milestone."""
    _append_orders(fresh_store, 10)
    a = (await svc.generate()).coupon.code
    b = (await svc.generate()).coupon.code
    other_store = InMemoryStore(Config())
    _append_orders(other_store, 5)
    c = (await CouponService(other_store, LockManager(other_store.products.keys()), Config()).generate()).coupon.code
    assert len({a, b, c}) == 3, "same milestone in two stores produced the same code: milestone-derived"
    for code in (a, b, c):
        assert isinstance(code, str) and len(code) >= 12
        assert not re.search(r"milestone|^\d+$", code, re.I)
        assert re.search(r"[0-9a-f]{12,}", code, re.I), "expected uuid4-derived hex material in the code"


async def test_only_one_coupon_per_milestone_under_concurrent_generate(svc: CouponService, fresh_store: InMemoryStore) -> None:
    """I7: N concurrent generate() at one milestone -> exactly one coupon, the rest NoEligibleMilestone."""
    _append_orders(fresh_store, 5)
    results = await asyncio.gather(*(svc.generate() for _ in range(20)), return_exceptions=True)
    ok = [r for r in results if isinstance(r, GenerationResult)]
    errs = [r for r in results if isinstance(r, NoEligibleMilestone)]
    assert len(ok) == 1 and len(errs) == 19, results
    assert len(fresh_store.coupons) == 1 and next(iter(fresh_store.coupons.values())).milestone == 1
    assert fresh_store.last_rewarded_milestone == 1
    _i15(fresh_store)


async def _free(locks: LockManager, **kw) -> bool:
    async def go() -> None:
        async with locks.acquire(**kw):
            pass
    task = asyncio.create_task(go())
    try:
        await asyncio.wait_for(asyncio.shield(task), T)
        return True
    except asyncio.TimeoutError:
        task.cancel()
        try:
            await task
        except BaseException:
            pass
        return False


async def test_generate_takes_only_the_coupon_ledger_lock(svc: CouponService, fresh_store: InMemoryStore, locks: LockManager) -> None:
    """TAD §3.8: generate acquires the ledger lock itself — and nothing else."""
    _append_orders(fresh_store, 5)
    entered, go = asyncio.Event(), asyncio.Event()

    async def hold_ledger() -> None:
        async with locks.acquire(coupon_ledger=True):
            entered.set()
            await go.wait()

    holder = asyncio.create_task(hold_ledger())
    await asyncio.wait_for(entered.wait(), T)
    gen = asyncio.create_task(svc.generate())
    for _ in range(10):
        await asyncio.sleep(0)
    assert not gen.done(), "generate did not block on the coupon ledger lock"
    assert await _free(locks, product_ids=["prd_dock"]) and await _free(locks, cart_id="c1")  # nothing else held
    go.set()
    await asyncio.wait_for(holder, T)
    assert (await asyncio.wait_for(gen, T)).coupon.milestone == 1

    # holding a product lock does not block generate
    entered2, go2 = asyncio.Event(), asyncio.Event()

    async def hold_product() -> None:
        async with locks.acquire(product_ids=["prd_dock"]):
            entered2.set()
            await go2.wait()

    _append_orders(fresh_store, 5)
    h2 = asyncio.create_task(hold_product())
    await asyncio.wait_for(entered2.wait(), T)
    assert (await asyncio.wait_for(svc.generate(), T)).coupon.milestone == 2
    go2.set()
    await asyncio.wait_for(h2, T)


# =========================================================================== reserve / commit / release


async def _one_coupon(svc: CouponService, store: InMemoryStore) -> Coupon:
    _append_orders(store, 5)
    return (await svc.generate()).coupon  # owner cus_2


def _err_bytes(exc: DomainError) -> bytes:
    return json.dumps({"code": exc.code.value, "message": exc.message, "details": exc.details}, sort_keys=True).encode()


async def test_unknown_code_and_wrong_owner_are_byte_identical_coupon_invalid(svc: CouponService, fresh_store: InMemoryStore) -> None:
    """A6 DoD / [D17] I6: never reveal that another customer's coupon exists."""
    coupon = await _one_coupon(svc, fresh_store)
    with pytest.raises(CouponInvalid) as unknown:
        svc.reserve_locked("NOPE-NOT-A-CODE", "cus_1")
    with pytest.raises(CouponInvalid) as wrong_owner:
        svc.reserve_locked(coupon.code, "cus_1")  # owner is cus_2
    assert _err_bytes(unknown.value) == _err_bytes(wrong_owner.value)
    assert str(unknown.value) == str(wrong_owner.value)
    assert unknown.value.http_status == wrong_owner.value.http_status == 422
    assert coupon.code not in str(wrong_owner.value) and coupon.code not in json.dumps(wrong_owner.value.details)
    assert coupon.state is CouponState.AVAILABLE  # nothing changed
    _i15(fresh_store)


async def test_reserve_available_by_owner(svc: CouponService, fresh_store: InMemoryStore) -> None:
    """[D30] AVAILABLE -> RESERVED, the stored coupon is returned."""
    coupon = await _one_coupon(svc, fresh_store)
    got = svc.reserve_locked(coupon.code, "cus_2")
    assert got is fresh_store.coupons[coupon.code]
    assert got.state is CouponState.RESERVED and got.redeemed_by_order_id is None
    _i15(fresh_store)


async def test_reserved_again_is_coupon_in_use(svc: CouponService, fresh_store: InMemoryStore) -> None:
    """I4 [D30]: RESERVED -> CouponInUse (409) for the owner; a non-owner still gets CouponInvalid."""
    coupon = await _one_coupon(svc, fresh_store)
    svc.reserve_locked(coupon.code, "cus_2")
    with pytest.raises(CouponInUse) as exc:
        svc.reserve_locked(coupon.code, "cus_2")
    assert exc.value.http_status == 409
    with pytest.raises(CouponInvalid):
        svc.reserve_locked(coupon.code, "cus_3")
    assert coupon.state is CouponState.RESERVED
    _i15(fresh_store)


async def test_commit_redeems_with_order_id(svc: CouponService, fresh_store: InMemoryStore) -> None:
    """I4: RESERVED -> REDEEMED with exactly one redeemed_by_order_id."""
    coupon = await _one_coupon(svc, fresh_store)
    svc.reserve_locked(coupon.code, "cus_2")
    svc.commit_locked(coupon.code, "ord_99")
    assert coupon.state is CouponState.REDEEMED and coupon.redeemed_by_order_id == "ord_99"
    _i15(fresh_store)
    with pytest.raises(CouponAlreadyRedeemed) as exc:
        svc.reserve_locked(coupon.code, "cus_2")
    assert exc.value.http_status == 422
    with pytest.raises(CouponInvalid):
        svc.reserve_locked(coupon.code, "cus_1")  # still indistinguishable for a non-owner
    assert coupon.redeemed_by_order_id == "ord_99"


async def test_release_returns_reserved_to_available(svc: CouponService, fresh_store: InMemoryStore) -> None:
    """I5 [D30]: RESERVED -> AVAILABLE; reservable again afterwards."""
    coupon = await _one_coupon(svc, fresh_store)
    svc.reserve_locked(coupon.code, "cus_2")
    svc.release_locked(coupon.code)
    assert coupon.state is CouponState.AVAILABLE and coupon.redeemed_by_order_id is None
    _i15(fresh_store)
    for _ in range(3):  # repeated reserve/release never loses the coupon
        svc.reserve_locked(coupon.code, "cus_2")
        svc.release_locked(coupon.code)
    assert coupon.state is CouponState.AVAILABLE
    assert svc.reserve_locked(coupon.code, "cus_2").state is CouponState.RESERVED


async def test_release_never_unredeems(svc: CouponService, fresh_store: InMemoryStore) -> None:
    """I4: a REDEEMED coupon stays REDEEMED whatever release_locked does."""
    coupon = await _one_coupon(svc, fresh_store)
    svc.reserve_locked(coupon.code, "cus_2")
    svc.commit_locked(coupon.code, "ord_1")
    try:
        svc.release_locked(coupon.code)
    except (DomainError, ValueError):
        pass
    assert coupon.state is CouponState.REDEEMED and coupon.redeemed_by_order_id == "ord_1"
    _i15(fresh_store)


async def test_commit_requires_reserved(svc: CouponService, fresh_store: InMemoryStore) -> None:
    """I4: committing a coupon that was never reserved must be refused, never silently redeemed."""
    coupon = await _one_coupon(svc, fresh_store)
    with pytest.raises((DomainError, ValueError)):
        svc.commit_locked(coupon.code, "ord_1")
    assert coupon.state is CouponState.AVAILABLE and coupon.redeemed_by_order_id is None
    _i15(fresh_store)


async def test_i15_across_full_lifecycle_of_two_coupons(svc: CouponService, fresh_store: InMemoryStore) -> None:
    _append_orders(fresh_store, 10)
    a = (await svc.generate()).coupon
    b = (await svc.generate()).coupon
    _i15(fresh_store)
    svc.reserve_locked(a.code, a.owner_customer_id)
    _i15(fresh_store)
    svc.reserve_locked(b.code, b.owner_customer_id)
    _i15(fresh_store)
    svc.commit_locked(a.code, "ord_a")
    _i15(fresh_store)
    svc.release_locked(b.code)
    _i15(fresh_store)
    states = sorted(c.state.value for c in await svc.list_coupons())
    assert states == ["AVAILABLE", "REDEEMED"]


# =========================================================================== HTTP


async def test_http_generate_201_then_409(app_client: httpx.AsyncClient, fresh_store: InMemoryStore, admin_headers: dict[str, str]) -> None:
    """[D14] POST /admin/coupons -> 201 {coupon, pending_milestones}; then 409 NO_ELIGIBLE_MILESTONE."""
    _append_orders(fresh_store, 5)
    resp = await app_client.post("/admin/coupons", headers=admin_headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body) == {"coupon", "pending_milestones"} and body["pending_milestones"] == 0
    assert body["coupon"]["milestone"] == 1 and body["coupon"]["owner_customer_id"] == "cus_2"
    assert body["coupon"]["state"] == "AVAILABLE" and body["coupon"]["percent"] == 10
    resp = await app_client.post("/admin/coupons", headers=admin_headers)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "NO_ELIGIBLE_MILESTONE"
    listed = await app_client.get("/admin/coupons", headers=admin_headers)
    assert listed.status_code == 200 and [c["code"] for c in listed.json()] == [body["coupon"]["code"]]
