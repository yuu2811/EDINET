"""Tender offer (公開買付/TOB) API endpoints."""

from fastapi import APIRouter, Query
from sqlalchemy import desc, func, select

from app.deps import get_async_session
from app.models import TenderOffer

router = APIRouter(prefix="/api/tob", tags=["TenderOffers"])


@router.get("")
async def list_tender_offers(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    """Return recent tender offer filings, newest first."""
    async with get_async_session()() as session:
        # Fetch count first so that total >= len(items) is always true
        # (new rows inserted between queries only increase the count).
        total = (await session.execute(
            select(func.count(TenderOffer.id))
        )).scalar()

        result = await session.execute(
            select(TenderOffer)
            .order_by(desc(TenderOffer.submit_date_time))
            .limit(limit)
            .offset(offset)
        )
        items = [t.to_dict() for t in result.scalars().all()]

    return {"items": items, "total": total}
