"""C1 — the harness proves itself.  Protects: every test below it (I1–I15 by way of isolation).

Three things this file must catch:
  1. `asyncio_mode = "auto"` not set → async tests silently skip and the suite reads green.
  2. A store leaking between tests → concurrency tests flake in ways that look like races.
  3. The app not being driven in-process on the test's own event loop / not using the
     fixture's store → HTTP-level assertions about store state are meaningless.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

# Module-level evidence that the async test body actually executed (not skipped).
_RAN: dict[str, bool] = {"async_body": False}
_STORE_IDS: list[int] = []


# --------------------------------------------------------------------------- 1. asyncio_mode


def test_asyncio_mode_is_auto(pytestconfig: pytest.Config) -> None:
    """C1 DoD: asyncio_mode = "auto" is configured (pyproject.toml)."""
    assert pytestconfig.getini("asyncio_mode") == "auto"


async def test_async_test_actually_runs() -> None:
    """C1 DoD: a trivial async test that actually runs — records evidence on the way through."""
    await asyncio.sleep(0)
    _RAN["async_body"] = True
    assert asyncio.get_running_loop() is not None


def test_async_test_was_not_silently_skipped() -> None:
    """C1 DoD: the async test above ran.  If it was skipped, this sync test fails loudly."""
    assert _RAN["async_body"] is True, (
        "the preceding async test did not execute — asyncio_mode is not 'auto' or "
        "pytest-asyncio is not installed; the async suite would be silently skipped"
    )


# --------------------------------------------------------------------------- 2. fresh store


def test_fresh_store_first_test_mutates(fresh_store) -> None:
    """C1 DoD: each test gets a fresh store.  This test deliberately dirties one."""
    _STORE_IDS.append(id(fresh_store))
    fresh_store.carts["__leak_sentinel__"] = object()
    assert "__leak_sentinel__" in fresh_store.carts


def test_fresh_store_second_test_sees_no_leak(fresh_store) -> None:
    """C1 DoD: the next test's store is a different object with none of the previous state.  I1."""
    assert _STORE_IDS, "ordering assumption broken: the mutating test did not run first"
    assert id(fresh_store) not in _STORE_IDS
    assert "__leak_sentinel__" not in fresh_store.carts
    assert fresh_store.carts == {}
    assert fresh_store.orders == []


def test_fresh_store_counters_match_seed(fresh_store, seeded_products) -> None:
    """I1 at t=0: stock_total == available + reserved + sold, with nothing reserved or sold."""
    assert set(fresh_store.available) == set(seeded_products)
    for pid, product in seeded_products.items():
        assert fresh_store.reserved[pid] == 0
        assert fresh_store.sold[pid] == 0
        assert fresh_store.available[pid] == product.stock_total
        assert product.stock_total == (
            fresh_store.available[pid] + fresh_store.reserved[pid] + fresh_store.sold[pid]
        )


# --------------------------------------------------------------------------- 3. in-process ASGI


async def test_app_client_is_in_process_asgi(app_client: httpx.AsyncClient) -> None:
    """C1 DoD: ASGITransport drives the app in-process on the test's own event loop."""
    assert isinstance(app_client._transport, httpx.ASGITransport)
    resp = await app_client.get("/products")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list) and len(body) == 6


async def test_app_client_uses_the_fixture_store(app_client: httpx.AsyncClient, fresh_store) -> None:
    """C1 DoD: the app is wired to THIS test's store — an HTTP write is visible in `fresh_store`."""
    assert fresh_store.carts == {}
    resp = await app_client.post("/carts")
    assert resp.status_code == 201
    cart_id = resp.json()["id"]
    assert cart_id in fresh_store.carts, "app is not using the fixture's store (per-request or global store?)"


async def test_app_state_does_not_leak_between_tests(app_client: httpx.AsyncClient, fresh_store, seeded_products) -> None:
    """C1 DoD: the cart created by the previous test is gone; products show full seed stock."""
    assert fresh_store.carts == {}
    resp = await app_client.get("/products")
    assert resp.status_code == 200
    by_id = {p["id"]: p for p in resp.json()}
    for pid, product in seeded_products.items():
        assert by_id[pid]["available"] == product.stock_total
        assert isinstance(by_id[pid]["unit_price_minor"], int)  # I14: no float in any response
