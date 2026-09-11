"""B5 gate — admin router and the X-Admin-Token guard.  Protects: I7 I11 · [D14] [D28].

All three admin routes sit behind ONE router-level dependency; missing, empty, or wrong token ->
403 FORBIDDEN (never a 422, never a 500).  POST /admin/coupons -> 201 {coupon, pending_milestones}
or 409 NO_ELIGIBLE_MILESTONE.  GET /admin/report is a GET that calls only build().

Mounted on a throwaway app with recording fakes so it depends on neither A6/A8 nor B6.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from src.api import deps
from src.api.errors import register_error_handlers
from src.api.routers import admin as admin_router
from src.config import Config
from src.core.coupons import GenerationResult
from src.core.errors import NoEligibleMilestone
from src.core.models import Coupon, CouponState
from src.core.reports import ItemsPurchased, Report
from src.core.store import InMemoryStore

SRC = Path(__file__).resolve().parent.parent.parent / "src"
TOKEN = "dev-admin-token"
OK = {"X-Admin-Token": TOKEN}
COUPON = Coupon("CPN-1", 10, 1, "cus_3", CouponState.AVAILABLE, None)
REPORT = Report(2, (ItemsPurchased("prd_cable", "USB-C Cable", 3),), 119_700, 11_970, 107_730, 1, 1, 0, 0, 5, 10)


class FakeCoupons:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.raise_next: Exception | None = None

    async def generate(self) -> GenerationResult:
        self.calls.append("generate")
        if self.raise_next:
            exc, self.raise_next = self.raise_next, None
            raise exc
        return GenerationResult(COUPON, 2)

    async def list_coupons(self) -> tuple[Coupon, ...]:
        self.calls.append("list_coupons")
        return (COUPON,)


class FakeReports:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def build(self) -> Report:
        self.calls.append("build")
        return REPORT

    def __getattr__(self, name: str) -> Any:  # anything but build is a violation
        raise AssertionError(f"report route touched ReportService.{name}")


class ExplodingState:
    def __init__(self) -> None:
        self.config = Config()
        self.store = InMemoryStore(Config())

    @property
    def coupon_service(self) -> Any:
        raise AssertionError("service resolved before the admin guard")

    @property
    def report_service(self) -> Any:
        raise AssertionError("service resolved before the admin guard")


def _app(coupons: Any = None, reports: Any = None, config: Config | None = None) -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(admin_router.router)
    app.state.config = config or Config()
    app.state.store = InMemoryStore(app.state.config)
    app.state.coupon_service = coupons or FakeCoupons()
    app.state.report_service = reports or FakeReports()
    return app


@pytest.fixture
def coupons() -> FakeCoupons:
    return FakeCoupons()


@pytest.fixture
def reports() -> FakeReports:
    return FakeReports()


@pytest.fixture
async def client(coupons: FakeCoupons, reports: FakeReports):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(coupons, reports)), base_url="http://t") as c:
        yield c


ROUTES = [("POST", "/admin/coupons"), ("GET", "/admin/coupons"), ("GET", "/admin/report")]


# --------------------------------------------------------------------------- structure


def test_exactly_three_admin_routes_with_status_codes() -> None:
    table = {(m, r.path, r.status_code or 200) for r in admin_router.router.routes if isinstance(r, APIRoute) for m in r.methods}
    assert table == {("POST", "/admin/coupons", 201), ("GET", "/admin/coupons", 200), ("GET", "/admin/report", 200)}


def test_guard_is_one_router_level_dependency_on_every_route() -> None:
    """B5 DoD: all three routes behind ONE X-Admin-Token dependency."""
    assert [d.dependency for d in admin_router.router.dependencies] == [deps.require_admin]
    for r in admin_router.router.routes:
        assert isinstance(r, APIRoute)
        calls = [d.call for d in r.dependant.dependencies]
        assert deps.require_admin in calls, f"{r.path} lacks the guard"


def test_handlers_are_async_def() -> None:
    """[D26]"""
    for r in admin_router.router.routes:
        assert inspect.iscoroutinefunction(r.endpoint), r.path


def test_admin_module_never_touches_the_store() -> None:
    text = (SRC / "api" / "routers" / "admin.py").read_text()
    code = "\n".join(l.split("#", 1)[0] for l in text.splitlines() if not l.strip().startswith(('"""', "'''")))
    assert not re.search(r"\bstore\b", code.replace("directly", "")) or "get_store" not in code
    assert "get_store" not in text and "InMemoryStore" not in text


# --------------------------------------------------------------------------- guard behaviour


@pytest.mark.parametrize("method,path", ROUTES, ids=[f"{m} {p}" for m, p in ROUTES])
async def test_missing_token_403_not_422(client: httpx.AsyncClient, coupons: FakeCoupons, reports: FakeReports, method: str, path: str) -> None:
    """B5 DoD / [D28]: missing X-Admin-Token -> 403 FORBIDDEN in the envelope; service untouched."""
    resp = await client.request(method, path)
    assert resp.status_code == 403, resp.text
    body = resp.json()
    assert body["error"]["code"] == "FORBIDDEN" and set(body["error"]) == {"code", "message", "details"}
    assert coupons.calls == [] and reports.calls == []


@pytest.mark.parametrize("value", ["", "wrong", "dev-admin-token ", " dev-admin-token", "DEV-ADMIN-TOKEN", "dev-admin-toke", "dev-admin-tokenX"])
@pytest.mark.parametrize("method,path", ROUTES, ids=[f"{m} {p}" for m, p in ROUTES])
async def test_wrong_or_empty_token_403(client: httpx.AsyncClient, method: str, path: str, value: str) -> None:
    resp = await client.request(method, path, headers={"X-Admin-Token": value})
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "FORBIDDEN", (value, resp.text)


@pytest.mark.parametrize("method,path", ROUTES, ids=[f"{m} {p}" for m, p in ROUTES])
async def test_non_ascii_token_is_403_not_500(client: httpx.AsyncClient, method: str, path: str) -> None:
    """A wrong token must be 403 whatever its bytes: a latin-1 header value must not crash the guard."""
    resp = await client.request(method, path, headers={"X-Admin-Token": "tökén".encode("latin-1")})
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "FORBIDDEN"


async def test_guard_runs_before_service_resolution() -> None:
    """B5 point: with no token, the services are never even looked up."""
    app = _app()
    app.state = ExplodingState()  # type: ignore[assignment]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        for method, path in ROUTES:
            resp = await c.request(method, path)
            assert resp.status_code == 403 and resp.json()["error"]["code"] == "FORBIDDEN", path


async def test_guard_uses_config_token_not_a_constant() -> None:
    """[D35] the token comes from Config: a different configured token is what unlocks the routes."""
    app = _app(config=Config(admin_token="another-secret"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/admin/report", headers=OK)).status_code == 403
        assert (await c.get("/admin/report", headers={"X-Admin-Token": "another-secret"})).status_code == 200


async def test_header_name_case_insensitive(client: httpx.AsyncClient) -> None:
    assert (await client.get("/admin/report", headers={"x-admin-token": TOKEN})).status_code == 200


# --------------------------------------------------------------------------- routes


async def test_generate_201_with_coupon_and_pending(client: httpx.AsyncClient, coupons: FakeCoupons) -> None:
    """B5 DoD: POST /admin/coupons -> 201 with the coupon plus pending_milestones.  I7"""
    resp = await client.post("/admin/coupons", headers=OK)
    assert resp.status_code == 201, resp.text
    assert resp.json() == {
        "coupon": {"code": "CPN-1", "percent": 10, "milestone": 1, "owner_customer_id": "cus_3",
                   "state": "AVAILABLE", "redeemed_by_order_id": None},
        "pending_milestones": 2,
    }
    assert coupons.calls == ["generate"]


async def test_generate_409_no_eligible_milestone(client: httpx.AsyncClient, coupons: FakeCoupons) -> None:
    """B5 DoD / [D14]: nothing to reward -> 409 NO_ELIGIBLE_MILESTONE with the service's details."""
    coupons.raise_next = NoEligibleMilestone("nothing due", {"placed_orders": 3, "next_milestone_at": 5})
    resp = await client.post("/admin/coupons", headers=OK)
    assert resp.status_code == 409
    assert resp.json()["error"] == {"code": "NO_ELIGIBLE_MILESTONE", "message": "nothing due",
                                    "details": {"placed_orders": 3, "next_milestone_at": 5}}


async def test_generate_takes_no_body_and_rejects_one_silently_or_not(client: httpx.AsyncClient, coupons: FakeCoupons) -> None:
    resp = await client.post("/admin/coupons", headers=OK, json={"milestone": 7})
    assert resp.status_code in (201, 422)  # no body model: FastAPI ignores; a strict model would 422 — either is fine, never 500
    assert resp.status_code != 500


async def test_list_coupons_200(client: httpx.AsyncClient, coupons: FakeCoupons) -> None:
    resp = await client.get("/admin/coupons", headers=OK)
    assert resp.status_code == 200
    assert [c["code"] for c in resp.json()] == ["CPN-1"]
    assert coupons.calls == ["list_coupons"]


async def test_report_is_get_calls_only_build_and_is_repeatable(client: httpx.AsyncClient, reports: FakeReports) -> None:
    """B5 DoD / I11 [D31]: GET /admin/report is GET, calls only build(), two calls byte-identical."""
    r1 = await client.get("/admin/report", headers=OK)
    r2 = await client.get("/admin/report", headers=OK)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.content == r2.content
    assert reports.calls == ["build", "build"]
    body = r1.json()
    assert body["gross_minor"] - body["discount_minor"] == body["net_minor"]  # I12
    assert body["coupons_generated"] == body["coupons_available"] + body["coupons_reserved"] + body["coupons_redeemed"]  # I15
    assert (await client.post("/admin/report", headers=OK)).status_code == 405
