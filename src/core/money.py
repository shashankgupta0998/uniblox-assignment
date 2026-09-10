"""Money arithmetic in int minor units. The only module permitted to import decimal. [TAD §3.2]

Pure, synchronous, no I-O, no locks. Rounding happens exactly once per order, here, on the
order-level discount, with ROUND_HALF_EVEN. [D1] [D2] [D3] [D4] [I9] [I12] [I14]
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from typing import Iterable

from src.core.models import OrderLine

_ONE_HUNDRED = Decimal(100)
_UNIT = Decimal(1)


def line_total_minor(unit_price_minor: int, quantity: int) -> int:
    """Integer multiplication. Nothing to round."""
    if unit_price_minor < 0 or quantity < 0:
        raise ValueError("unit_price_minor and quantity must be non-negative")
    return unit_price_minor * quantity


def gross_minor(lines: Iterable[OrderLine]) -> int:
    """Sum of line totals. Integer addition. Nothing to round."""
    return sum(line.line_total_minor for line in lines)


def discount_minor(gross: int, percent: int) -> int:
    """Decimal(gross) * percent/100, quantized to 1 with ROUND_HALF_EVEN, int(),
    then min(result, gross). Raises ValueError if percent not in [0, 100].  [D2][D3][D4]"""
    if percent < 0 or percent > 100:
        raise ValueError(f"percent must be in [0, 100], got {percent}")
    if gross < 0:
        raise ValueError(f"gross must be non-negative, got {gross}")
    scaled = (Decimal(gross) * Decimal(percent) / _ONE_HUNDRED).quantize(_UNIT, rounding=ROUND_HALF_EVEN)
    return min(int(scaled), gross)


def net_minor(gross: int, discount: int) -> int:
    """gross - discount. Asserts 0 <= discount <= gross.  [I9][I12]"""
    assert 0 <= discount <= gross, f"discount {discount} outside [0, {gross}]"
    return gross - discount
