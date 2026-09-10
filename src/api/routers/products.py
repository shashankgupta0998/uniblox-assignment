"""GET /products and GET /customers. Lock-free reads of seed data and counters. [TAD §3.6] [TAD §8]"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from src.api.deps import get_store
from src.api.schemas import CustomerResponse, ProductResponse
from src.core.store import InMemoryStore

router = APIRouter(tags=["catalogue"])

Store = Annotated[InMemoryStore, Depends(get_store)]


@router.get("/products", response_model=list[ProductResponse])
async def list_products(store: Store) -> list[ProductResponse]:
    """Every seeded product with its live counters. `available` is what a checkout can still win;
    all four numbers are exposed so a client can check I1 for itself."""
    return [
        ProductResponse(
            id=product.id,
            name=product.name,
            unit_price_minor=product.unit_price_minor,
            stock_total=product.stock_total,
            available=store.available[product.id],
            reserved=store.reserved[product.id],
            sold=store.sold[product.id],
        )
        for product in store.products.values()
    ]


@router.get("/customers", response_model=list[CustomerResponse])
async def list_customers(store: Store) -> list[CustomerResponse]:
    """The seeded customer ids, for the demo harness. There is no authentication. [D11]"""
    return [CustomerResponse.model_validate(customer) for customer in store.customers.values()]
