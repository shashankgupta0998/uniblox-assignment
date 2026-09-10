"""Cart routes. Every mutation goes through CartService, which takes the locks. [TAD §3.7] [TAD §8]

Every handler is `async def`: a `def` handler would run in a threadpool where asyncio.Lock protects
nothing. [D26]
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from src.api.deps import get_cart_service
from src.api.schemas import AddItemRequest, CartResponse, ErrorEnvelope, SetQuantityRequest
from src.core.carts import CartService

router = APIRouter(prefix="/carts", tags=["carts"])

Carts = Annotated[CartService, Depends(get_cart_service)]

_NOT_FOUND = {404: {"model": ErrorEnvelope}}
_MUTATION_ERRORS = {
    404: {"model": ErrorEnvelope},
    409: {"model": ErrorEnvelope},
    422: {"model": ErrorEnvelope},
}


@router.post("", status_code=status.HTTP_201_CREATED, response_model=CartResponse)
async def create_cart(carts: Carts) -> CartResponse:
    """A new, empty, OPEN cart. No customer is attached until checkout. [D11]"""
    return CartResponse.model_validate(await carts.create_cart())


@router.get("/{cart_id}", response_model=CartResponse, responses=_NOT_FOUND)
async def get_cart(cart_id: str, carts: Carts) -> CartResponse:
    """The cart with each line's snapshot price, live price, and `price_changed`."""
    return CartResponse.model_validate(await carts.get_cart(cart_id))


@router.post(
    "/{cart_id}/items",
    status_code=status.HTTP_201_CREATED,
    response_model=CartResponse,
    responses=_MUTATION_ERRORS,
)
async def add_item(cart_id: str, body: AddItemRequest, carts: Carts) -> CartResponse:
    """Add a line, or increment an existing one; either way the price is re-snapshotted. [D5]"""
    return CartResponse.model_validate(await carts.add_item(cart_id, body.product_id, body.quantity))


@router.put("/{cart_id}/items/{product_id}", response_model=CartResponse, responses=_MUTATION_ERRORS)
async def set_item_quantity(
    cart_id: str, product_id: str, body: SetQuantityRequest, carts: Carts
) -> CartResponse:
    """Set an absolute quantity and re-snapshot the price."""
    return CartResponse.model_validate(await carts.set_item_quantity(cart_id, product_id, body.quantity))


@router.delete(
    "/{cart_id}/items/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses=_MUTATION_ERRORS,
)
async def remove_item(cart_id: str, product_id: str, carts: Carts) -> Response:
    """Remove the line. Empty body."""
    await carts.remove_item(cart_id, product_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{cart_id}/reprice", response_model=CartResponse, responses=_MUTATION_ERRORS)
async def reprice(cart_id: str, carts: Carts) -> CartResponse:
    """The customer explicitly accepts the current prices: every line is re-snapshotted. [D6]"""
    return CartResponse.model_validate(await carts.reprice(cart_id))
