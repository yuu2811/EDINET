"""Dashboard statistics endpoint with lightweight TTL cache."""

import time
from datetime import date, datetime

from fastapi import APIRouter, Query
from sqlalchemy import case, desc, func, select

from app.config import JST, settings
from app.deps import get_async_session
from app.models import Filing
from app.poller import broadcaster

router = APIRouter(tags=["Stats"])

# Lightweight TTL cache for stats responses (avoids hammering DB on every dashboard refresh)
_stats_cache: dict[str, tuple[float, dict]] = {}
_STATS_CACHE_TTL = 5.0  # seconds — short enough to feel real-time


@router.get("/api/stats")
async def get_stats(
    target_date: str | None = Query(None, alias="date", description="Date (YYYY-MM-DD)"),
) -> dict:
    """Get statistics for the dashboard."""
    if target_date:
        try:
            today = date.fromisoformat(target_date)
        except ValueError:
            today = datetime.now(JST).date()
    else:
        today = datetime.now(JST).date()
    today_str = today.strftime("%Y-%m-%d")

    # Check cache
    now = time.monotonic()
    cached = _stats_cache.get(today_str)
    if cached and (now - cached[0]) < _STATS_CACHE_TTL:
        result = cached[1].copy()
        # Always return live client count (not cached)
        result["connected_clients"] = broadcaster.client_count
        return result

    async with get_async_session()() as session:
        # Consolidated query: total, new_reports, amendments in a single round-trip
        date_filter = Filing.submit_date_time.startswith(today_str)
        summary_q = select(
            func.count(Filing.id).label("today_total"),
            func.sum(case(
                (Filing.is_amendment.is_(False), 1), else_=0,
            )).label("new_reports"),
            func.sum(case(
                (Filing.is_amendment.is_(True), 1), else_=0,
            )).label("amendments"),
        ).where(date_filter)
        summary = (await session.execute(summary_q)).one()

        total = (
            await session.execute(select(func.count(Filing.id)))
        ).scalar()

        top_filers_q = (
            select(
                Filing.filer_name,
                Filing.edinet_code,
                func.count(Filing.id).label("cnt"),
            )
            .where(date_filter)
            .group_by(Filing.filer_name, Filing.edinet_code)
            .order_by(desc("cnt"))
            .limit(10)
        )
        top_filers_result = await session.execute(top_filers_q)
        top_filers = [
            {"name": row[0], "edinet_code": row[1], "count": row[2]}
            for row in top_filers_result
        ]

        result = {
            "date": today_str,
            "today_total": summary.today_total or 0,
            "today_new_reports": summary.new_reports or 0,
            "today_amendments": summary.amendments or 0,
            "total_in_db": total,
            "top_filers": top_filers,
            "connected_clients": broadcaster.client_count,
            "poll_interval": settings.POLL_INTERVAL,
        }

        _stats_cache[today_str] = (now, result)
        return result
