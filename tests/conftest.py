"""C1 — Test harness.  [FTL C1]  Protects: every test in the suite.

Fixtures
--------
config            Config with a non-zero payment latency so an in-flight window is real.
fresh_store       A brand-new InMemoryStore per test.  Never shared, never reused.
seeded_products   {product_id: Product} from src.config.SEED_PRODUCTS.
fake_gateway      FakePaymentGateway(latency_seconds=PAYMENT_LATENCY) — always approves.
declining_gateway AlwaysDeclineGateway() — always raises PaymentDeclined.
make_app          callable(config, store, payments) -> FastAPI, one app around ONE store.
app_client        httpx.AsyncClient over ASGITransport, in-process, on the test's own loop,
                  wired to `fresh_store` and `fake_gateway`.
client_factory    async context manager for a client around a *different* store/gateway
                  (e.g. `declining_gateway`) when a test needs more than one app.
admin_headers     {"X-Admin-Token": config.admin_token}

Why every `src` import is lazy (inside the fixture)
---------------------------------------------------
A1's DoD is "pytest collects zero tests without import errors", and src/main.py does not exist
until B6.  A module-level `from src.main import ...` here would turn a missing B6 into a
collection error for the whole suite.  Importing inside the fixture keeps collection clean and
turns "not built yet" into a red *test*, which is the correct state.

App-factory seam required from B6 (not in TAD §3, stated here once)
-------------------------------------------------------------------
    src.main.create_app(config: Config, *, store: InMemoryStore, payments: PaymentGateway) -> FastAPI

All singletons (store, LockManager, IdempotencyRegistry, services) are built inside
`create_app` — NOT in a lifespan handler — because ASGITransport does not run lifespan
events.  The module-level `app` for uvicorn is `create_app(load_config(), ...)`.
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable

import httpx
import pytest

if TYPE_CHECKING:  # pragma: no cover — typing only; no src import at collection time
    from src.config import Config

# Make `src` importable from the repo root regardless of how pytest was invoked.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Non-zero so the payment await is a real yield point.  Tests that need a wider window
# build their own gateway with a larger latency via `client_factory`.  [D25]
PAYMENT_LATENCY: float = 0.02

BASE_URL = "http://testserver"


# --------------------------------------------------------------------------- config / store


@pytest.fixture
def config() -> "Config":
    from src.config import Config

    return Config(payment_latency_seconds=PAYMENT_LATENCY)


@pytest.fixture
def fresh_store(config: "Config"):
    """A new InMemoryStore for this test only.  I1–I15 all assume this isolation."""
    from src.core.store import InMemoryStore

    return InMemoryStore(config)


@pytest.fixture
def seeded_products() -> dict[str, Any]:
    from src.config import SEED_PRODUCTS

    return {p.id: p for p in SEED_PRODUCTS}


@pytest.fixture
def seeded_customers() -> dict[str, Any]:
    from src.config import SEED_CUSTOMERS

    return {c.id: c for c in SEED_CUSTOMERS}


# --------------------------------------------------------------------------- gateways


@pytest.fixture
def fake_gateway():
    from src.core.payments import FakePaymentGateway

    return FakePaymentGateway(latency_seconds=PAYMENT_LATENCY)


@pytest.fixture
def declining_gateway():
    from src.core.payments import AlwaysDeclineGateway

    return AlwaysDeclineGateway()


# --------------------------------------------------------------------------- app + client


def _build_app(config: "Config", store: Any, payments: Any):
    """The one place the harness touches src.main.  Fails loudly, never skips."""
    try:
        from src.main import create_app
    except ImportError as exc:  # B6 not built, or seam missing
        pytest.fail(
            "src.main.create_app is not importable — B6 app factory not built yet, or it does "
            f"not expose create_app(config, *, store, payments). ImportError: {exc}"
        )
    return create_app(config, store=store, payments=payments)


@pytest.fixture
def make_app() -> Callable[..., Any]:
    return _build_app


@contextlib.asynccontextmanager
async def _client_for(config: "Config", store: Any, payments: Any) -> AsyncIterator[httpx.AsyncClient]:
    app = _build_app(config, store, payments)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
        yield client


@pytest.fixture
async def app_client(config: "Config", fresh_store: Any, fake_gateway: Any) -> AsyncIterator[httpx.AsyncClient]:
    """In-process ASGI client bound to THIS test's `fresh_store` and an approving gateway."""
    async with _client_for(config, fresh_store, fake_gateway) as client:
        yield client


@pytest.fixture
def client_factory(config: "Config") -> Callable[..., Any]:
    """`async with client_factory(store=..., payments=..., config=...) as client:`"""

    def factory(*, store: Any, payments: Any, config: "Config" = config):
        return _client_for(config, store, payments)

    return factory


@pytest.fixture
def admin_headers(config: "Config") -> dict[str, str]:
    return {"X-Admin-Token": config.admin_token}
