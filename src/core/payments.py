"""PaymentGateway protocol + FakePaymentGateway + AlwaysDeclineGateway. [TAD §3.5] [D25]

`FakePaymentGateway.charge` always awaits `asyncio.sleep`, even at latency 0. That await is the
yield point the product locks exist to guard: without it the checkout critical section would be
trivially atomic and the concurrency design would be untestable.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Protocol

from src.core.errors import PaymentDeclined


@dataclass(frozen=True)
class PaymentResult:
    reference: str


class PaymentGateway(Protocol):
    async def charge(self, *, amount_minor: int, order_ref: str) -> PaymentResult:
        """Raises PaymentDeclined on failure."""
        ...


class FakePaymentGateway:
    def __init__(self, latency_seconds: float = 0.0) -> None:
        self._latency_seconds: float = latency_seconds

    @property
    def latency_seconds(self) -> float:
        return self._latency_seconds

    async def charge(self, *, amount_minor: int, order_ref: str) -> PaymentResult:
        """Always succeeds. Always yields to the event loop first, even at latency 0. [D25]"""
        await asyncio.sleep(self._latency_seconds)
        return PaymentResult(reference=f"pay_{uuid.uuid4().hex}")


class AlwaysDeclineGateway:
    async def charge(self, *, amount_minor: int, order_ref: str) -> PaymentResult:
        """Always declines, after the same yield point, so the release path is exercised realistically."""
        await asyncio.sleep(0)
        raise PaymentDeclined("Payment declined by the gateway.", {"order_ref": order_ref, "amount_minor": amount_minor})
