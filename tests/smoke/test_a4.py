"""A4 smoke gate — IdempotencyRegistry.  Protects: I3 · [D19] [D20] [D21] [D22].

Directly against src/core/idempotency.py, no HTTP.
  - claim is SYNCHRONOUS: the module contains no `await` and no `async`; claim/complete/release
    are not coroutine functions.  That is the whole mechanism (TAD §5).
  - Fingerprint mismatch is checked BEFORE the in-progress check.
  - fingerprint is stable across key ordering and equals sha256 of the canonical JSON.
  - release fully removes the record.
  - Records are keyed by (cart_id, key).
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
from collections.abc import Mapping
from pathlib import Path

import pytest

from src.core.idempotency import ClaimResult, ClaimStatus, IdempotencyRegistry

SRC = Path(__file__).resolve().parent.parent.parent / "src"
FP_A = "fp-a"
FP_B = "fp-b"


@pytest.fixture
def reg() -> IdempotencyRegistry:
    return IdempotencyRegistry()


# --------------------------------------------------------------------------- synchronous by construction


def test_module_has_no_await_and_no_async() -> None:
    """A4 DoD: `grep "await" src/core/idempotency.py` returns nothing; nor does async."""
    text = (SRC / "core" / "idempotency.py").read_text()
    code = "\n".join(l.split("#", 1)[0] for l in text.splitlines())
    assert not re.search(r"\bawait\b", code)
    assert not re.search(r"\basync\b", code)
    assert "asyncio" not in code and "threading" not in code


def test_claim_complete_release_are_plain_def() -> None:
    for name in ("claim", "complete", "release", "fingerprint"):
        fn = inspect.getattr_static(IdempotencyRegistry, name)
        fn = fn.__func__ if isinstance(fn, staticmethod) else fn
        assert not inspect.iscoroutinefunction(fn), name
        assert not inspect.isasyncgenfunction(fn), name


# --------------------------------------------------------------------------- fingerprint


def test_fingerprint_is_sha256_of_canonical_json() -> None:
    """[D20] sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")))."""
    payload = {"cart_id": "crt_1", "customer_id": "cus_1", "coupon_code": None}
    expected = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    got = IdempotencyRegistry.fingerprint(payload)
    assert got == expected
    assert isinstance(got, str) and re.fullmatch(r"[0-9a-f]{64}", got)


def test_fingerprint_stable_across_key_ordering() -> None:
    """A4 DoD: same payload, different insertion order -> same fingerprint."""
    a = {"cart_id": "crt_1", "customer_id": "cus_1", "coupon_code": "CPN"}
    b = {"coupon_code": "CPN", "customer_id": "cus_1", "cart_id": "crt_1"}
    c = dict(reversed(list(a.items())))
    assert IdempotencyRegistry.fingerprint(a) == IdempotencyRegistry.fingerprint(b) == IdempotencyRegistry.fingerprint(c)


def test_fingerprint_changes_when_any_field_changes() -> None:
    base = {"cart_id": "crt_1", "customer_id": "cus_1", "coupon_code": "CPN"}
    fp = IdempotencyRegistry.fingerprint(base)
    assert IdempotencyRegistry.fingerprint({**base, "coupon_code": None}) != fp
    assert IdempotencyRegistry.fingerprint({**base, "coupon_code": "cpn"}) != fp
    assert IdempotencyRegistry.fingerprint({**base, "customer_id": "cus_2"}) != fp
    assert IdempotencyRegistry.fingerprint({**base, "cart_id": "crt_2"}) != fp


def test_fingerprint_is_pure() -> None:
    p = {"x": 1}
    assert IdempotencyRegistry.fingerprint(p) == IdempotencyRegistry.fingerprint(p)
    assert p == {"x": 1}


# --------------------------------------------------------------------------- claim state machine


def test_absent_claim_is_granted_and_records_in_progress(reg: IdempotencyRegistry) -> None:
    """TAD §3.4: absent -> insert IN_PROGRESS, return GRANTED."""
    r = reg.claim("crt_1", "k", FP_A)
    assert r == ClaimResult(ClaimStatus.GRANTED)
    assert r.order_id is None
    assert reg.claim("crt_1", "k", FP_A) == ClaimResult(ClaimStatus.IN_PROGRESS)


def test_in_progress_same_fingerprint(reg: IdempotencyRegistry) -> None:
    """[D21] a concurrent replay of an in-flight key -> IN_PROGRESS, never a second GRANTED."""
    assert reg.claim("crt_1", "k", FP_A).status is ClaimStatus.GRANTED
    for _ in range(5):
        r = reg.claim("crt_1", "k", FP_A)
        assert r.status is ClaimStatus.IN_PROGRESS and r.order_id is None


def test_mismatch_is_checked_before_in_progress(reg: IdempotencyRegistry) -> None:
    """A4 DoD / [D20]: a different fingerprint against an IN_PROGRESS record -> FINGERPRINT_MISMATCH."""
    assert reg.claim("crt_1", "k", FP_A).status is ClaimStatus.GRANTED
    r = reg.claim("crt_1", "k", FP_B)
    assert r.status is ClaimStatus.FINGERPRINT_MISMATCH, r
    assert r.order_id is None
    # and the original record is untouched: same fingerprint still reports IN_PROGRESS
    assert reg.claim("crt_1", "k", FP_A).status is ClaimStatus.IN_PROGRESS


def test_complete_then_same_fingerprint_is_replay_with_order_id(reg: IdempotencyRegistry) -> None:
    """[D23] COMPLETED + matching fingerprint -> REPLAY(order_id)."""
    reg.claim("crt_1", "k", FP_A)
    reg.complete("crt_1", "k", "ord_1")
    r = reg.claim("crt_1", "k", FP_A)
    assert r == ClaimResult(ClaimStatus.REPLAY, "ord_1")
    assert reg.claim("crt_1", "k", FP_A) == ClaimResult(ClaimStatus.REPLAY, "ord_1")  # stays replayable


def test_complete_then_different_fingerprint_is_mismatch(reg: IdempotencyRegistry) -> None:
    """[D20] COMPLETED + different fingerprint -> FINGERPRINT_MISMATCH, no order_id leaked."""
    reg.claim("crt_1", "k", FP_A)
    reg.complete("crt_1", "k", "ord_1")
    r = reg.claim("crt_1", "k", FP_B)
    assert r.status is ClaimStatus.FINGERPRINT_MISMATCH
    assert r.order_id is None
    assert reg.claim("crt_1", "k", FP_A) == ClaimResult(ClaimStatus.REPLAY, "ord_1")


def _record_keys(reg: IdempotencyRegistry) -> list[object]:
    keys: list[object] = []
    for value in vars(reg).values():
        if isinstance(value, Mapping):
            keys.extend(value.keys())
    return keys


def test_release_fully_removes_the_record(reg: IdempotencyRegistry) -> None:
    """A4 DoD / [D22]: after release a retry is a genuine new attempt — even with a DIFFERENT fingerprint."""
    assert reg.claim("crt_1", "k", FP_A).status is ClaimStatus.GRANTED
    assert _record_keys(reg), "expected an internal record after claim"
    reg.release("crt_1", "k")
    assert not any("crt_1" in str(k) and "k" in str(k) for k in _record_keys(reg)), _record_keys(reg)
    assert reg.claim("crt_1", "k", FP_B).status is ClaimStatus.GRANTED  # no lingering fingerprint
    reg.release("crt_1", "k")
    assert reg.claim("crt_1", "k", FP_A).status is ClaimStatus.GRANTED


def test_release_after_complete_also_removes(reg: IdempotencyRegistry) -> None:
    reg.claim("crt_1", "k", FP_A)
    reg.complete("crt_1", "k", "ord_1")
    reg.release("crt_1", "k")
    assert reg.claim("crt_1", "k", FP_A).status is ClaimStatus.GRANTED


def test_release_of_absent_record_is_harmless(reg: IdempotencyRegistry) -> None:
    """The except path may release a key that was never claimed (claim failed early); must not raise."""
    reg.release("crt_1", "never-claimed")
    assert reg.claim("crt_1", "never-claimed", FP_A).status is ClaimStatus.GRANTED


# --------------------------------------------------------------------------- scope: (cart_id, key)


def test_same_key_on_two_carts_is_independent(reg: IdempotencyRegistry) -> None:
    """[D19] scope is (cart_id, key): a lazy client reusing `1` on every cart is safe."""
    assert reg.claim("crt_1", "1", FP_A).status is ClaimStatus.GRANTED
    assert reg.claim("crt_2", "1", FP_B).status is ClaimStatus.GRANTED
    reg.complete("crt_1", "1", "ord_1")
    assert reg.claim("crt_1", "1", FP_A) == ClaimResult(ClaimStatus.REPLAY, "ord_1")
    assert reg.claim("crt_2", "1", FP_B).status is ClaimStatus.IN_PROGRESS
    reg.release("crt_2", "1")
    assert reg.claim("crt_2", "1", FP_B).status is ClaimStatus.GRANTED
    assert reg.claim("crt_1", "1", FP_A) == ClaimResult(ClaimStatus.REPLAY, "ord_1")  # untouched


def test_two_keys_on_one_cart_are_independent_records(reg: IdempotencyRegistry) -> None:
    """The registry alone does not enforce one-checkout-per-cart (that is I2, the cart state)."""
    assert reg.claim("crt_1", "k1", FP_A).status is ClaimStatus.GRANTED
    assert reg.claim("crt_1", "k2", FP_A).status is ClaimStatus.GRANTED
    reg.complete("crt_1", "k1", "ord_1")
    assert reg.claim("crt_1", "k2", FP_A).status is ClaimStatus.IN_PROGRESS


def test_complete_records_the_given_order_id_only(reg: IdempotencyRegistry) -> None:
    reg.claim("crt_1", "k", FP_A)
    reg.complete("crt_1", "k", "ord_42")
    assert reg.claim("crt_1", "k", FP_A).order_id == "ord_42"


# --------------------------------------------------------------------------- atomicity under the event loop


async def test_concurrent_claims_grant_exactly_once(reg: IdempotencyRegistry) -> None:
    """I3 at the registry: 200 coroutines racing claim() for one (cart, key) -> exactly one GRANTED."""
    results: list[ClaimResult] = []

    async def racer() -> None:
        await asyncio.sleep(0)
        results.append(reg.claim("crt_1", "k", FP_A))

    await asyncio.gather(*(racer() for _ in range(200)))
    statuses = [r.status for r in results]
    assert statuses.count(ClaimStatus.GRANTED) == 1
    assert statuses.count(ClaimStatus.IN_PROGRESS) == 199


async def test_concurrent_mixed_fingerprints(reg: IdempotencyRegistry) -> None:
    """Racing claims with two fingerprints: one GRANTED; its twins IN_PROGRESS; the others MISMATCH."""
    results: list[tuple[str, ClaimResult]] = []

    async def racer(fp: str) -> None:
        await asyncio.sleep(0)
        results.append((fp, reg.claim("crt_1", "k", fp)))

    await asyncio.gather(*(racer(FP_A if i % 2 else FP_B) for i in range(100)))
    granted = [fp for fp, r in results if r.status is ClaimStatus.GRANTED]
    assert len(granted) == 1
    winner = granted[0]
    for fp, r in results:
        if r.status is ClaimStatus.GRANTED:
            continue
        assert r.status is (ClaimStatus.IN_PROGRESS if fp == winner else ClaimStatus.FINGERPRINT_MISMATCH), (fp, r)


def test_claim_result_is_frozen_value_object() -> None:
    r = ClaimResult(ClaimStatus.GRANTED)
    with pytest.raises(Exception):
        r.status = ClaimStatus.REPLAY  # type: ignore[misc]
    assert ClaimResult(ClaimStatus.REPLAY, "o") == ClaimResult(ClaimStatus.REPLAY, "o")
