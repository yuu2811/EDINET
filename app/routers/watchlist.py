"""Watchlist CRUD and watchlist-filings endpoints."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import desc, or_, select

from app.deps import get_async_session
from app.models import Filing, Watchlist
from app.schemas import WatchlistCreate

router = APIRouter(prefix="/api/watchlist", tags=["Watchlist"])


@router.get("")
async def get_watchlist() -> dict:
    """Get the user's watchlist."""
    async with get_async_session()() as session:
        result = await session.execute(
            select(Watchlist).order_by(Watchlist.created_at)
        )
        items = result.scalars().all()
        return {"watchlist": [w.to_dict() for w in items]}


@router.post("")
async def add_to_watchlist(body: WatchlistCreate) -> dict:
    """Add a company to the watchlist."""
    async with get_async_session()() as session:
        item = Watchlist(
            company_name=body.company_name.strip(),
            sec_code=body.sec_code.strip() if body.sec_code else None,
            edinet_code=body.edinet_code.strip() if body.edinet_code else None,
        )
        session.add(item)
        await session.commit()
        await session.refresh(item)
        return item.to_dict()


# Static path MUST be registered before parameterized path
# to prevent "/api/watchlist/filings" matching "/{item_id}"
@router.get("/filings")
async def get_watchlist_filings() -> dict:
    """Get recent filings matching the watchlist."""
    async with get_async_session()() as session:
        wl_result = await session.execute(select(Watchlist))
        watchlist = wl_result.scalars().all()

        if not watchlist:
            return {"filings": []}

        conditions = []
        for w in watchlist:
            if w.sec_code:
                conditions.append(Filing.target_sec_code == w.sec_code)
                conditions.append(Filing.sec_code == w.sec_code)
            if w.edinet_code:
                conditions.append(Filing.subject_edinet_code == w.edinet_code)
                conditions.append(Filing.issuer_edinet_code == w.edinet_code)
            if w.company_name:
                conditions.append(
                    Filing.target_company_name.contains(w.company_name)
                )
                conditions.append(Filing.filer_name.contains(w.company_name))

        if not conditions:
            return {"filings": []}

        query = (
            select(Filing)
            .where(or_(*conditions))
            .distinct()
            .order_by(desc(Filing.submit_date_time))
            .limit(50)
        )
        result = await session.execute(query)
        filings = result.scalars().all()

        return {"filings": [f.to_dict() for f in filings]}


@router.delete("/{item_id}")
async def remove_from_watchlist(item_id: int) -> dict:
    """Remove a company from the watchlist."""
    async with get_async_session()() as session:
        result = await session.execute(
            select(Watchlist).where(Watchlist.id == item_id)
        )
        item = result.scalar_one_or_none()
        if not item:
            return JSONResponse({"error": "Not found"}, status_code=404)
        await session.delete(item)
        await session.commit()
        return {"status": "deleted"}
