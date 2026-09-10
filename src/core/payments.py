"""PaymentGateway protocol + FakePaymentGateway + AlwaysDeclineGateway. [TAD §3.5] [D25]"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


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

    async def charge(self, *, amount_minor: int, order_ref: str) -> PaymentResult:
        raise NotImplementedError


class AlwaysDeclineGateway:
    async def charge(self, *, amount_minor: int, order_ref: str) -> PaymentResult:
        raise NotImplementedError
