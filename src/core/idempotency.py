"""IdempotencyRegistry: atomic claim / complete / release. [TAD §3.4] [TAD §5]"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class ClaimStatus(str, Enum):
    GRANTED = "GRANTED"
    REPLAY = "REPLAY"
    IN_PROGRESS = "IN_PROGRESS"
    FINGERPRINT_MISMATCH = "FINGERPRINT_MISMATCH"


@dataclass(frozen=True)
class ClaimResult:
    status: ClaimStatus
    order_id: str | None = None


class IdempotencyRegistry:
    def claim(self, cart_id: str, key: str, fingerprint: str) -> ClaimResult:
        """SYNCHRONOUS and therefore atomic — no await, so no interleaving.
        Absent      -> insert IN_PROGRESS, return GRANTED
        IN_PROGRESS -> return IN_PROGRESS                       [D21]
        COMPLETED   -> fingerprint match: REPLAY(order_id)      [D23]
                       mismatch:          FINGERPRINT_MISMATCH  [D20]
        Note: a mismatch against an IN_PROGRESS record also returns
        FINGERPRINT_MISMATCH — checked before the in-progress check."""
        raise NotImplementedError

    def complete(self, cart_id: str, key: str, order_id: str) -> None:
        raise NotImplementedError

    def release(self, cart_id: str, key: str) -> None:
        """Drop the record so a retry after failure is a genuine new attempt.  [D22]"""
        raise NotImplementedError

    @staticmethod
    def fingerprint(payload: Mapping[str, Any]) -> str:
        """sha256 of json.dumps(payload, sort_keys=True, separators=(',',':')).  [D20]"""
        raise NotImplementedError
