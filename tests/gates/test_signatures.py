"""A1 gate — every TAD.md §3 (and §2) signature exists VERBATIM.  Protects: the interface contract.

FTL A1 DoD: "Every §3 signature exists verbatim — argument names, keyword-only markers, and return
types included."  A drift here is a FAIL even when the code works, because B and C build against
this contract.  Every `_locked` / `claim` / `acquire` method must be `def`; every service entry
point the TAD marks `async def` must be a coroutine function.

Each case is (dotted path, expected signature string as written in TAD.md, is_async).
Annotations are normalised (module prefixes, whitespace, Optional[T] -> T | None) before comparison
so that `from __future__ import annotations` and `typing` vs `collections.abc` spellings do not
produce false drift; anything else that differs IS drift.
"""
from __future__ import annotations

import dataclasses
import enum
import importlib
import inspect
import re
from typing import Any

import pytest

# --------------------------------------------------------------------------- normalisation

_PREFIXES = (
    "src.core.errors.", "src.core.models.", "src.core.money.", "src.core.locks.",
    "src.core.idempotency.", "src.core.payments.", "src.core.store.", "src.core.carts.",
    "src.core.coupons.", "src.core.checkout.", "src.core.reports.", "src.config.",
    "typing.", "collections.abc.", "contextlib.", "builtins.",
)


def _norm_ann(ann: Any) -> str:
    if ann is inspect.Parameter.empty:
        return "<none>"
    s = ann if isinstance(ann, str) else inspect.formatannotation(ann)
    for p in _PREFIXES:
        s = s.replace(p, "")
    s = re.sub(r"\s+", "", s)
    s = s.replace("'", "").replace('"', "")
    m = re.fullmatch(r"Optional\[(.*)\]", s)
    if m:
        s = f"{m.group(1)}|None"
    # Union[a, None] -> a|None
    m = re.fullmatch(r"Union\[(.*),None\]", s)
    if m:
        s = f"{m.group(1)}|None"
    return s


def _describe(fn: Any) -> str:
    """Render a callable's signature into the TAD's textual form, normalised."""
    sig = inspect.signature(fn)
    parts: list[str] = []
    saw_kw_only_marker = False
    for p in sig.parameters.values():
        if p.kind is inspect.Parameter.KEYWORD_ONLY and not saw_kw_only_marker:
            parts.append("*")
            saw_kw_only_marker = True
        if p.kind is inspect.Parameter.VAR_KEYWORD:
            parts.append(f"**{p.name}")
            continue
        if p.kind is inspect.Parameter.VAR_POSITIONAL:
            parts.append(f"*{p.name}")
            saw_kw_only_marker = True
            continue
        txt = p.name
        if p.annotation is not inspect.Parameter.empty:
            txt += f":{_norm_ann(p.annotation)}"
        if p.default is not inspect.Parameter.empty:
            txt += f"={p.default!r}"
        parts.append(txt)
    ret = _norm_ann(sig.return_annotation) if sig.return_annotation is not inspect.Signature.empty else "<none>"
    return f"({','.join(parts)})->{ret}"


def _expect(text: str) -> str:
    """Normalise the TAD's own text the same way (so the table can be written verbatim)."""
    head, _, ret = text.partition("->")
    head = re.sub(r"\s+", "", head)
    inner = head[1:-1]
    # normalise each parameter's annotation and default the way _describe does
    out: list[str] = []
    depth = 0
    buf = ""
    for ch in inner:
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(buf)
            buf = ""
        else:
            buf += ch
    if buf:
        out.append(buf)
    norm_parts = []
    for part in out:
        if part == "*" or part.startswith("*"):
            norm_parts.append(part)
            continue
        name, _, rest = part.partition(":")
        ann, _, default = rest.partition("=")
        txt = name
        if ann:
            txt += f":{_norm_ann(ann)}"
        if default:
            txt += f"={eval(default)!r}"  # literal defaults only: (), False, None, 0.0
        norm_parts.append(txt)
    ret = _norm_ann(ret.strip()) if ret.strip() else "<none>"
    return f"({','.join(norm_parts)})->{ret}"


def _resolve(path: str) -> Any:
    mod_name, _, attr = path.rpartition(".")
    # class attribute paths look like  src.core.locks.LockManager.acquire
    try:
        mod = importlib.import_module(mod_name)
        return getattr(mod, attr)
    except ModuleNotFoundError:
        mod_name2, _, cls_name = mod_name.rpartition(".")
        mod = importlib.import_module(mod_name2)
        return getattr(getattr(mod, cls_name), attr)


# --------------------------------------------------------------------------- the table

S = "self,"
FUNCTIONS: list[tuple[str, str, bool]] = [
    # 3.1 errors
    ("src.core.errors.DomainError.__init__",
     f"({S} message: str, details: dict[str, Any] | None = None) -> None", False),
    # 3.2 money
    ("src.core.money.line_total_minor", "(unit_price_minor: int, quantity: int) -> int", False),
    ("src.core.money.gross_minor", "(lines: Iterable[OrderLine]) -> int", False),
    ("src.core.money.discount_minor", "(gross: int, percent: int) -> int", False),
    ("src.core.money.net_minor", "(gross: int, discount: int) -> int", False),
    # 3.3 locks
    ("src.core.locks.LockManager.__init__", f"({S} product_ids: Iterable[str]) -> None", False),
    ("src.core.locks.LockManager.acquire",
     f"({S} *, product_ids: Sequence[str] = (), coupon_ledger: bool = False, cart_id: str | None = None)"
     " -> AbstractAsyncContextManager[None]", False),
    # 3.4 idempotency
    ("src.core.idempotency.IdempotencyRegistry.claim",
     f"({S} cart_id: str, key: str, fingerprint: str) -> ClaimResult", False),
    ("src.core.idempotency.IdempotencyRegistry.complete",
     f"({S} cart_id: str, key: str, order_id: str) -> None", False),
    ("src.core.idempotency.IdempotencyRegistry.release", f"({S} cart_id: str, key: str) -> None", False),
    ("src.core.idempotency.IdempotencyRegistry.fingerprint", "(payload: Mapping[str, Any]) -> str", False),
    # 3.5 payments
    ("src.core.payments.PaymentGateway.charge",
     f"({S} *, amount_minor: int, order_ref: str) -> PaymentResult", True),
    ("src.core.payments.FakePaymentGateway.__init__", f"({S} latency_seconds: float = 0.0) -> None", False),
    ("src.core.payments.FakePaymentGateway.charge",
     f"({S} *, amount_minor: int, order_ref: str) -> PaymentResult", True),
    ("src.core.payments.AlwaysDeclineGateway.charge",
     f"({S} *, amount_minor: int, order_ref: str) -> PaymentResult", True),
    # 3.6 store
    ("src.core.store.InMemoryStore.__init__", f"({S} config: Config) -> None", False),
    ("src.core.store.InMemoryStore.reserve_inventory_locked",
     f"({S} product_id: str, quantity: int) -> None", False),
    ("src.core.store.InMemoryStore.commit_inventory_locked",
     f"({S} product_id: str, quantity: int) -> None", False),
    ("src.core.store.InMemoryStore.release_inventory_locked",
     f"({S} product_id: str, quantity: int) -> None", False),
    ("src.core.store.InMemoryStore.append_order_locked", f"({S} order: Order) -> None", False),
    ("src.core.store.InMemoryStore.placed_order_count", f"({S[:-1]}) -> int", False),
    ("src.core.store.InMemoryStore.available_quantity", f"({S} product_id: str) -> int", False),
    # 3.7 carts
    ("src.core.carts.CartService.__init__", f"({S} store: InMemoryStore, locks: LockManager) -> None", False),
    ("src.core.carts.CartService.create_cart", f"({S[:-1]}) -> CartView", True),
    ("src.core.carts.CartService.get_cart", f"({S} cart_id: str) -> CartView", True),
    ("src.core.carts.CartService.add_item",
     f"({S} cart_id: str, product_id: str, quantity: int) -> CartView", True),
    ("src.core.carts.CartService.set_item_quantity",
     f"({S} cart_id: str, product_id: str, quantity: int) -> CartView", True),
    ("src.core.carts.CartService.remove_item", f"({S} cart_id: str, product_id: str) -> CartView", True),
    ("src.core.carts.CartService.reprice", f"({S} cart_id: str) -> CartView", True),
    # 3.8 coupons
    ("src.core.coupons.CouponService.__init__",
     f"({S} store: InMemoryStore, locks: LockManager, config: Config) -> None", False),
    ("src.core.coupons.CouponService.generate", f"({S[:-1]}) -> GenerationResult", True),
    ("src.core.coupons.CouponService.list_coupons", f"({S[:-1]}) -> tuple[Coupon, ...]", True),
    ("src.core.coupons.CouponService.reserve_locked", f"({S} code: str, customer_id: str) -> Coupon", False),
    ("src.core.coupons.CouponService.commit_locked", f"({S} code: str, order_id: str) -> None", False),
    ("src.core.coupons.CouponService.release_locked", f"({S} code: str) -> None", False),
    # 3.9 checkout
    ("src.core.checkout.CheckoutService.__init__",
     f"({S} store: InMemoryStore, locks: LockManager, idempotency: IdempotencyRegistry, "
     "coupons: CouponService, payments: PaymentGateway, config: Config) -> None", False),
    ("src.core.checkout.CheckoutService.checkout",
     f"({S} *, cart_id: str, customer_id: str, coupon_code: str | None, idempotency_key: str)"
     " -> CheckoutResult", True),
    # 3.10 reports
    ("src.core.reports.ReportService.__init__", f"({S} store: InMemoryStore, config: Config) -> None", False),
    ("src.core.reports.ReportService.build", f"({S[:-1]}) -> Report", True),
    # 3.11 config
    ("src.config.load_config", "() -> Config", False),
]

DATACLASSES: list[tuple[str, tuple[str, ...], bool]] = [
    # §2 models                                                          frozen?
    ("src.core.models.Product", ("id", "name", "unit_price_minor", "stock_total"), True),
    ("src.core.models.Customer", ("id", "name"), True),
    ("src.core.models.CartItem", ("product_id", "quantity", "unit_price_minor"), False),
    ("src.core.models.Cart", ("id", "state", "items"), False),
    ("src.core.models.OrderLine",
     ("product_id", "product_name", "quantity", "unit_price_minor", "line_total_minor"), True),
    ("src.core.models.Order",
     ("id", "sequence", "cart_id", "customer_id", "lines", "gross_minor", "discount_minor",
      "net_minor", "coupon_code", "discount_percent", "state"), True),
    ("src.core.models.Coupon",
     ("code", "percent", "milestone", "owner_customer_id", "state", "redeemed_by_order_id"), False),
    # §3 view / result objects
    ("src.core.idempotency.ClaimResult", ("status", "order_id"), True),
    ("src.core.payments.PaymentResult", ("reference",), True),
    ("src.core.carts.CartLineView",
     ("product_id", "product_name", "quantity", "unit_price_minor", "line_total_minor",
      "current_unit_price_minor", "price_changed"), True),
    ("src.core.carts.CartView", ("id", "state", "lines", "gross_minor", "has_price_changes"), True),
    ("src.core.coupons.GenerationResult", ("coupon", "pending_milestones"), True),
    ("src.core.checkout.CheckoutResult", ("order", "replayed"), True),
    ("src.core.reports.ItemsPurchased", ("product_id", "name", "quantity"), True),
    ("src.core.reports.Report",
     ("orders_placed", "items_purchased", "gross_minor", "discount_minor", "net_minor",
      "coupons_generated", "coupons_available", "coupons_reserved", "coupons_redeemed", "n", "x"), True),
    ("src.config.Config", ("n", "x", "admin_token", "payment_latency_seconds", "currency"), True),
]

ENUMS: list[tuple[str, tuple[str, ...]]] = [
    ("src.core.errors.ErrorCode", (
        "VALIDATION_FAILED", "CART_NOT_FOUND", "CART_EMPTY", "CART_ALREADY_CHECKED_OUT", "CART_MODIFIED",
        "PRODUCT_NOT_FOUND", "QUANTITY_EXCEEDS_STOCK", "INSUFFICIENT_INVENTORY", "PRICE_CHANGED",
        "UNKNOWN_CUSTOMER", "COUPON_INVALID", "COUPON_ALREADY_REDEEMED", "COUPON_IN_USE",
        "NO_ELIGIBLE_MILESTONE", "IDEMPOTENCY_KEY_REQUIRED", "IDEMPOTENCY_KEY_REUSED",
        "REQUEST_IN_PROGRESS", "PAYMENT_DECLINED", "ORDER_NOT_FOUND", "FORBIDDEN", "INTERNAL_ERROR")),
    ("src.core.models.CartState", ("OPEN", "CHECKED_OUT")),
    ("src.core.models.CouponState", ("AVAILABLE", "RESERVED", "REDEEMED")),
    ("src.core.idempotency.ClaimStatus", ("GRANTED", "REPLAY", "IN_PROGRESS", "FINGERPRINT_MISMATCH")),
]


# --------------------------------------------------------------------------- tests


@pytest.mark.parametrize("path,expected,is_async", FUNCTIONS, ids=[f[0].removeprefix("src.") for f in FUNCTIONS])
def test_signature_verbatim(path: str, expected: str, is_async: bool) -> None:
    """A1 DoD: signature verbatim — names, keyword-only markers, defaults, annotations, return type."""
    fn = _resolve(path)
    if isinstance(fn, staticmethod):
        fn = fn.__func__
    assert callable(fn), f"{path} is not callable"
    got = _describe(fn)
    want = _expect(expected)
    assert got == want, f"\n{path}\n  observed: {got}\n  TAD §3:   {want}"
    assert inspect.iscoroutinefunction(fn) == is_async, (
        f"{path}: TAD says {'async def' if is_async else 'def'}; "
        f"observed {'async def' if inspect.iscoroutinefunction(fn) else 'def'}"
    )


def test_fingerprint_is_a_staticmethod() -> None:
    """TAD §3.4: `fingerprint` is a @staticmethod taking only `payload`."""
    from src.core.idempotency import IdempotencyRegistry

    assert isinstance(inspect.getattr_static(IdempotencyRegistry, "fingerprint"), staticmethod)


@pytest.mark.parametrize("path,fields,frozen", DATACLASSES, ids=[d[0].removeprefix("src.") for d in DATACLASSES])
def test_dataclass_fields_verbatim(path: str, fields: tuple[str, ...], frozen: bool) -> None:
    """TAD §2/§3: dataclass field names, order, and frozen-ness exactly as written."""
    cls = _resolve(path)
    assert dataclasses.is_dataclass(cls), f"{path} is not a dataclass"
    got = tuple(f.name for f in dataclasses.fields(cls))
    assert got == fields, f"\n{path}\n  observed: {got}\n  TAD:      {fields}"
    assert cls.__dataclass_params__.frozen is frozen, f"{path}: frozen should be {frozen}"


@pytest.mark.parametrize("path,members", ENUMS, ids=[e[0].removeprefix("src.") for e in ENUMS])
def test_enum_members_verbatim(path: str, members: tuple[str, ...]) -> None:
    """TAD §3.1/§3.4/§2: enum members exactly, str-valued, value == name."""
    cls = _resolve(path)
    assert issubclass(cls, enum.Enum) and issubclass(cls, str), f"{path} must be a (str, Enum)"
    assert tuple(m.name for m in cls) == members, f"\n{path}\n  observed: {tuple(m.name for m in cls)}\n  TAD: {members}"
    for m in cls:
        assert m.value == m.name


def test_config_defaults_verbatim() -> None:
    """TAD §3.11: Config defaults n=5, x=10, admin_token='dev-admin-token', latency 0.0, 'INR'."""
    from src.config import Config

    defaults = {f.name: f.default for f in dataclasses.fields(Config)}
    assert defaults == {
        "n": 5, "x": 10, "admin_token": "dev-admin-token", "payment_latency_seconds": 0.0, "currency": "INR",
    }


def test_seed_data_shape() -> None:
    """A1 DoD: SEED_PRODUCTS has the six PRD 2.1 products; SEED_CUSTOMERS has cus_1..cus_5."""
    from src.config import SEED_CUSTOMERS, SEED_PRODUCTS
    from src.core.models import Customer, Product

    assert isinstance(SEED_PRODUCTS, tuple) and len(SEED_PRODUCTS) == 6
    assert all(isinstance(p, Product) for p in SEED_PRODUCTS)
    by_id = {p.id: p for p in SEED_PRODUCTS}
    expected = {
        "prd_keyboard": (449900, 25), "prd_mouse": (129900, 40), "prd_monitor": (1899900, 10),
        "prd_cable": (39900, 100), "prd_dock": (2499900, 3), "prd_headset": (899900, 1),
    }
    assert set(by_id) == set(expected)
    for pid, (price, stock) in expected.items():
        assert (by_id[pid].unit_price_minor, by_id[pid].stock_total) == (price, stock), pid
        assert type(by_id[pid].unit_price_minor) is int  # I14
    assert isinstance(SEED_CUSTOMERS, tuple) and len(SEED_CUSTOMERS) == 5
    assert all(isinstance(c, Customer) for c in SEED_CUSTOMERS)
    assert tuple(c.id for c in SEED_CUSTOMERS) == ("cus_1", "cus_2", "cus_3", "cus_4", "cus_5")


def test_store_declares_its_attributes() -> None:
    """TAD §3.6: InMemoryStore declares the ten public attributes as class annotations."""
    from src.core.store import InMemoryStore

    want = {"products", "customers", "carts", "orders", "orders_by_id", "coupons",
            "last_rewarded_milestone", "available", "reserved", "sold"}
    have = set(getattr(InMemoryStore, "__annotations__", {}))
    assert want <= have, f"missing class annotations: {sorted(want - have)}"


def test_core_imports_nothing_from_api_or_fastapi() -> None:
    """TAD §1: dependency direction is one-way api -> core. core has no FastAPI import."""
    from pathlib import Path

    core = Path(__file__).resolve().parent.parent.parent / "src" / "core"
    offenders = []
    for py in sorted(core.glob("*.py")):
        text = py.read_text()
        if re.search(r"^\s*(from|import)\s+(fastapi|starlette|src\.api|pydantic)\b", text, re.M):
            offenders.append(py.name)
    assert offenders == [], f"core imports api/fastapi/pydantic in: {offenders}"
    for name in ("src.core.checkout", "src.core.store", "src.core.carts", "src.core.coupons", "src.core.reports"):
        importlib.import_module(name)
