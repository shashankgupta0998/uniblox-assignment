"""Money arithmetic in int minor units. The only module permitted to import decimal. [TAD §3.2]

Pure, synchronous, no I/O, no locks. [D1] [D2] [D3] [D4]
"""

from __future__ import annotations

from typing import Iterable

from src.core.models import OrderLine


def line_total_minor(unit_price_minor: int, quantity: int) -> int:
    raise NotImplementedError


def gross_minor(lines: Iterable[OrderLine]) -> int:
    raise NotImplementedError


def discount_minor(gross: int, percent: int) -> int:
    """Decimal(gross) * percent / 100, quantized to 1 with ROUND_HALF_EVEN, int(),
    then min(result, gross). Raises ValueError if percent not in [0, 100].  [D2][D3][D4]"""
    raise NotImplementedError


def net_minor(gross: int, discount: int) -> int:
    """gross - discount. Asserts 0 <= discount <= gross.  [I9][I12]"""
    raise NotImplementedError
