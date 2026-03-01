"""Watchlist CRUD and watchlist-filings endpoints."""

from fastapi import APIRouter, HTTPException
from sqlalchemy import desc, func, or_, select

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
    """Add a company to the watchlist (with duplicate detection)."""
    async with get_async_session()() as session:
        name = body.company_name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="企業名は必須です")
        sec_code = body.sec_code.strip() if body.sec_code else None

        # M5: Check for duplicates by company_name or sec_code
        conditions = [func.lower(Watchlist.company_name) == name.lower()]
        if sec_code:
            conditions.append(Watchlist.sec_code == sec_code)
        existing = await session.execute(
            select(Watchlist).where(or_(*conditions))
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=409,
                detail="この銘柄は既にウォッチリストに登録されています",
            )

        item = Watchlist(
            company_name=name,
            sec_code=sec_code,
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

        # Collect unique values for batch IN() queries instead of
        # individual OR conditions per watchlist item.
        sec_codes = {w.sec_code for w in watchlist if w.sec_code}
        edinet_codes = {w.edinet_code for w in watchlist if w.edinet_code}
        company_names = [w.company_name for w in watchlist if w.company_name]

        conditions = []
        if sec_codes:
            codes = list(sec_codes)
            conditions.append(Filing.target_sec_code.in_(codes))
            conditions.append(Filing.sec_code.in_(codes))
        if edinet_codes:
            codes = list(edinet_codes)
            conditions.append(Filing.subject_edinet_code.in_(codes))
            conditions.append(Filing.issuer_edinet_code.in_(codes))
        for name in company_names:
            conditions.append(Filing.target_company_name.contains(name))
            conditions.append(Filing.filer_name.contains(name))

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
            raise HTTPException(status_code=404, detail="ウォッチリスト項目が見つかりません")
        await session.delete(item)
        await session.commit()
        return {"status": "deleted"}
