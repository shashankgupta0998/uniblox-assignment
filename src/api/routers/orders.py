"""GET /orders/{order_id}. A lock-free read: orders are immutable once appended. [TAD §3.6] [I10]"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from src.api.deps import get_store
from src.api.schemas import ErrorEnvelope, OrderResponse
from src.core.errors import OrderNotFound
from src.core.store import InMemoryStore

router = APIRouter(prefix="/orders", tags=["orders"])

Store = Annotated[InMemoryStore, Depends(get_store)]


@router.get("/{order_id}", response_model=OrderResponse, responses={404: {"model": ErrorEnvelope}})
async def get_order(order_id: str, store: Store) -> OrderResponse:
    """The order exactly as it was placed: its own price snapshot, never recomputed."""
    order = store.orders_by_id.get(order_id)
    if order is None:
        raise OrderNotFound("No such order.", {"order_id": order_id})
    return OrderResponse.model_validate(order)
