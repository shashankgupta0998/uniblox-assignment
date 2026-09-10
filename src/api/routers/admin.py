"""Administrative routes: coupon generation, coupon listing, the report. [TAD §8] [D14] [D28] [D31]

All three sit behind one router-level `require_admin` dependency. None touches the store directly.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from src.api.deps import get_coupon_service, get_report_service, require_admin
from src.api.schemas import CouponGenerationResponse, CouponResponse, ErrorEnvelope, ReportResponse
from src.core.coupons import CouponService
from src.core.reports import ReportService

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
    responses={403: {"model": ErrorEnvelope}},
)

Coupons = Annotated[CouponService, Depends(get_coupon_service)]
Reports = Annotated[ReportService, Depends(get_report_service)]


@router.post(
    "/coupons",
    status_code=status.HTTP_201_CREATED,
    response_model=CouponGenerationResponse,
    responses={409: {"model": ErrorEnvelope}},
)
async def generate_coupon(coupons: Coupons) -> CouponGenerationResponse:
    """Reward the lowest unrewarded reached milestone with one coupon. 409 NO_ELIGIBLE_MILESTONE
    when nothing is due. Milestone k is rewarded once, ever. [I7] [D14]"""
    return CouponGenerationResponse.model_validate(await coupons.generate())


@router.get("/coupons", response_model=list[CouponResponse])
async def list_coupons(coupons: Coupons) -> list[CouponResponse]:
    """Every coupon with its state and, once redeemed, the order that redeemed it."""
    return [CouponResponse.model_validate(coupon) for coupon in await coupons.list_coupons()]


@router.get("/report", response_model=ReportResponse)
async def get_report(reports: Reports) -> ReportResponse:
    """A pure read. Mutates nothing; two identical calls return identical bodies. [I11] [D31]"""
    return ReportResponse.model_validate(await reports.build())
