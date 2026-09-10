"""IdempotencyRegistry: atomic claim / complete / release. [TAD §3.4] [TAD §5]

The whole module is synchronous. `claim` is the mechanism: it is an insert-if-absent with no
yield point inside, so on a single event loop the check and the insert cannot interleave. It runs
before any lock is taken. The event loop is the mutex for that one function. [D19]–[D22]

Records live for the process lifetime; only failures remove them. [D22]
"""

from __future__ import annotations

import hashlib
import json
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


class _RecordState(str, Enum):
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"


@dataclass
class _Record:
    state: _RecordState
    fingerprint: str  # sha256 of canonical {cart_id, customer_id, coupon_code}
    order_id: str | None


class IdempotencyRegistry:
    def __init__(self) -> None:
        self._records: dict[tuple[str, str], _Record] = {}

    def claim(self, cart_id: str, key: str, fingerprint: str) -> ClaimResult:
        """SYNCHRONOUS and therefore atomic — no yield point, so no interleaving.
        Absent      -> insert IN_PROGRESS, return GRANTED
        IN_PROGRESS -> return IN_PROGRESS                       [D21]
        COMPLETED   -> fingerprint match: REPLAY(order_id)      [D23]
                       mismatch:          FINGERPRINT_MISMATCH  [D20]
        Note: a mismatch against an IN_PROGRESS record also returns
        FINGERPRINT_MISMATCH — checked before the in-progress check."""
        record = self._records.get((cart_id, key))
        if record is None:
            self._records[(cart_id, key)] = _Record(_RecordState.IN_PROGRESS, fingerprint, None)
            return ClaimResult(ClaimStatus.GRANTED)
        if record.fingerprint != fingerprint:
            return ClaimResult(ClaimStatus.FINGERPRINT_MISMATCH)
        if record.state is _RecordState.IN_PROGRESS:
            return ClaimResult(ClaimStatus.IN_PROGRESS)
        return ClaimResult(ClaimStatus.REPLAY, order_id=record.order_id)

    def complete(self, cart_id: str, key: str, order_id: str) -> None:
        """Mark the claimed record COMPLETED with the order it produced. Requires a live claim."""
        record = self._records.get((cart_id, key))
        if record is None or record.state is not _RecordState.IN_PROGRESS:
            raise ValueError(f"no in-progress claim for ({cart_id!r}, {key!r})")
        record.state = _RecordState.COMPLETED
        record.order_id = order_id

    def release(self, cart_id: str, key: str) -> None:
        """Drop the record so a retry after failure is a genuine new attempt.  [D22]"""
        self._records.pop((cart_id, key), None)

    @staticmethod
    def fingerprint(payload: Mapping[str, Any]) -> str:
        """sha256 of json.dumps(payload, sort_keys=True, separators=(',',':')).  [D20]"""
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
