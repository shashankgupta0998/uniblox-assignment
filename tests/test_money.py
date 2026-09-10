"""C2 — Money and rounding.  Protects: I9 I12 I14 · [D2] [D3] [D4].

All expected values are derived with INTEGER arithmetic only (divmod), so no float appears in any
assertion and the oracle cannot itself suffer binary rounding.  ROUND_HALF_EVEN is reproduced as:
    q, r = divmod(gross * percent, 100)
    r*2 > 100 -> q+1 ; r*2 < 100 -> q ; tie -> q if q is even else q+1
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from src.core import money
from src.core.models import OrderLine

SRC = Path(__file__).resolve().parent.parent / "src"


def half_even(gross: int, percent: int) -> int:
    """Integer oracle for Decimal(gross*percent/100).quantize(1, ROUND_HALF_EVEN)."""
    q, r = divmod(gross * percent, 100)
    if r * 2 > 100:
        return q + 1
    if r * 2 < 100:
        return q
    return q if q % 2 == 0 else q + 1


def half_up(gross: int, percent: int) -> int:
    q, r = divmod(gross * percent, 100)
    return q + 1 if r * 2 >= 100 else q


# --------------------------------------------------------------------------- line / gross


def test_line_total_is_exact_product() -> None:
    """I14: line total is unit * quantity in int minor units."""
    assert money.line_total_minor(39_900, 3) == 119_700
    assert money.line_total_minor(899_900, 1) == 899_900
    assert type(money.line_total_minor(1, 100)) is int


def test_gross_sums_line_totals() -> None:
    """I14 / I12: gross is the exact integer sum of line totals."""
    lines = (
        OrderLine("prd_cable", "USB-C Cable", 3, 39_900, 119_700),
        OrderLine("prd_headset", "Noise-Cancelling Headset", 1, 899_900, 899_900),
    )
    assert money.gross_minor(lines) == 1_019_600
    assert money.gross_minor(()) == 0
    assert type(money.gross_minor(lines)) is int


# --------------------------------------------------------------------------- half-even


@pytest.mark.parametrize(
    "gross,percent,expected_half_even,expected_half_up",
    [
        (25, 10, 2, 3),        # 2.5  -> 2 (even)   half-up says 3   <- they DISAGREE
        (5, 10, 0, 1),         # 0.5  -> 0 (even)   half-up says 1   <- they DISAGREE
        (35, 10, 4, 4),        # 3.5  -> 4 (even)   both agree
        (15, 10, 2, 2),        # 1.5  -> 2 (even)   both agree
        (45, 10, 4, 5),        # 4.5  -> 4           DISAGREE
        (1_250, 1, 12, 13),    # 12.5 -> 12          DISAGREE
        (1_350, 1, 14, 14),    # 13.5 -> 14          agree
        (12_345, 3, 370, 370), # 370.35 -> 370       not a tie: plain rounding down
        (12_355, 3, 371, 371), # 370.65 -> 371       not a tie: plain rounding up
    ],
)
def test_round_half_to_even_at_both_boundaries(gross: int, percent: int, expected_half_even: int, expected_half_up: int) -> None:
    """[D3] ROUND_HALF_EVEN: .5 goes to the even neighbour, both downward (2.5->2) and upward (3.5->4)."""
    got = money.discount_minor(gross, percent)
    assert got == expected_half_even
    assert got == half_even(gross, percent)
    assert type(got) is int
    if expected_half_even != expected_half_up:
        assert got != half_up(gross, percent), "this is a case where half-even and half-up disagree"


def test_half_even_disagrees_with_half_up_somewhere() -> None:
    """C2 DoD: at least one asserted case where round-half-to-even and half-up differ."""
    assert money.discount_minor(25, 10) == 2
    assert half_up(25, 10) == 3


# --------------------------------------------------------------------------- bounds and clamp


@pytest.mark.parametrize("gross", [0, 1, 2, 3, 99, 100, 101, 449_900, 2_499_900, 1_019_600])
def test_percent_zero_and_hundred(gross: int) -> None:
    """[D4] percent=0 -> 0 discount; percent=100 -> discount == gross exactly (the clamp bound)."""
    assert money.discount_minor(gross, 0) == 0
    assert money.discount_minor(gross, 100) == gross
    assert money.net_minor(gross, money.discount_minor(gross, 100)) == 0
    assert money.net_minor(gross, money.discount_minor(gross, 0)) == gross


@pytest.mark.parametrize("percent", [-1, 101, 1_000, -100])
def test_percent_outside_range_rejected(percent: int) -> None:
    """[D4] discount_minor raises ValueError for percent outside [0, 100]."""
    with pytest.raises(ValueError):
        money.discount_minor(10_000, percent)


def test_discount_never_exceeds_gross_clamp() -> None:
    """I9 / [D4]: discount_minor <= gross for every legal percent, including 100 on odd gross."""
    for gross in (0, 1, 3, 7, 999, 449_901):
        for percent in range(0, 101):
            d = money.discount_minor(gross, percent)
            assert 0 <= d <= gross, (gross, percent, d)


def test_net_rejects_discount_above_gross_or_negative() -> None:
    """I9: net_minor asserts 0 <= discount <= gross."""
    with pytest.raises((AssertionError, ValueError)):
        money.net_minor(100, 101)
    with pytest.raises((AssertionError, ValueError)):
        money.net_minor(100, -1)


# --------------------------------------------------------------------------- the property


TABLE = [
    (gross, percent)
    for gross in (0, 1, 5, 25, 99, 100, 101, 12_345, 39_900, 129_900, 449_900, 899_900, 1_899_900, 2_499_900, 7_631_407)
    for percent in (0, 1, 3, 7, 10, 12, 33, 50, 66, 99, 100)
]


@pytest.mark.parametrize("gross,percent", TABLE)
def test_gross_minus_discount_equals_net(gross: int, percent: int) -> None:
    """I12: gross - discount == net exactly, for every row; I9 bounds; I14 int throughout."""
    discount = money.discount_minor(gross, percent)
    net = money.net_minor(gross, discount)
    assert gross - discount == net
    assert 0 <= discount <= gross
    assert net >= 0
    assert discount == half_even(gross, percent)
    assert type(discount) is int and type(net) is int


# --------------------------------------------------------------------------- source hygiene


def _code_lines(path: Path) -> list[tuple[int, str]]:
    """Source lines with docstrings and comments removed, so the grep sees code only."""
    text = path.read_text()
    tree = ast.parse(text)
    doc_ranges: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                    and isinstance(body[0].value.value, str):
                doc_ranges.append((body[0].lineno, body[0].end_lineno))
    out = []
    for i, line in enumerate(text.splitlines(), start=1):
        if any(a <= i <= b for a, b in doc_ranges):
            continue
        code = line.split("#", 1)[0]
        if code.strip():
            out.append((i, code))
    return out


def test_money_py_has_no_float_division_or_round() -> None:
    """A2 DoD: `grep -rn "float\\|[^_]/ \\|round(" src/core/money.py` returns only the Decimal quantize.
    I14 / [D1]: no float, no `/` on money, no round() on a float."""
    offenders = []
    for lineno, code in _code_lines(SRC / "core" / "money.py"):
        if re.search(r"\bfloat\b|round\(", code):
            offenders.append((lineno, code.strip()))
        if re.search(r"[^_]/ |//", code) and "Decimal" not in code:
            offenders.append((lineno, code.strip()))
    assert offenders == [], f"float / division / round in money.py outside the Decimal path: {offenders}"


def test_decimal_is_confined_to_money_py() -> None:
    """TAD §3.2: money.py is the only module permitted to import decimal.  [D2]"""
    leaks = []
    for py in sorted(SRC.rglob("*.py")):
        if py.name == "money.py":
            continue
        for lineno, code in _code_lines(py):
            if re.search(r"^\s*(from|import)\s+decimal\b|\bDecimal\b", code):
                leaks.append((str(py.relative_to(SRC)), lineno))
    assert leaks == [], f"Decimal escaped money.py: {leaks}"
    assert any("decimal" in code for _, code in _code_lines(SRC / "core" / "money.py")), "money.py must use decimal"


def test_this_file_contains_no_float_literal() -> None:
    """C2 DoD: no float anywhere in the assertions — enforced on this very file."""
    tree = ast.parse(Path(__file__).read_text())
    floats = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, float)]
    assert floats == []
