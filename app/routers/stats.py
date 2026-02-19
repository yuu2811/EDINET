"""Dashboard statistics endpoint."""

from datetime import date

from fastapi import APIRouter, Query
from sqlalchemy import desc, func, select

from app.config import settings
from app.models import Filing
from app.poller import broadcaster

router = APIRouter(tags=["Stats"])


def _get_async_session():
    """Resolve async_session at runtime via app.main for testability."""
    import app.main
    return app.main.async_session


@router.get("/api/stats")
async def get_stats(
    target_date: str | None = Query(None, alias="date", description="Date (YYYY-MM-DD)"),
) -> dict:
    """Get statistics for the dashboard."""
    if target_date:
        try:
            today = date.fromisoformat(target_date)
        except ValueError:
            today = date.today()
    else:
        today = date.today()
    today_str = today.strftime("%Y-%m-%d")

    async with _get_async_session()() as session:
        today_count = (
            await session.execute(
                select(func.count(Filing.id)).where(
                    Filing.submit_date_time.startswith(today_str)
                )
            )
        ).scalar()

        new_reports = (
            await session.execute(
                select(func.count(Filing.id)).where(
                    Filing.submit_date_time.startswith(today_str),
                    Filing.is_amendment.is_(False),
                )
            )
        ).scalar()

        amendments = (
            await session.execute(
                select(func.count(Filing.id)).where(
                    Filing.submit_date_time.startswith(today_str),
                    Filing.is_amendment.is_(True),
                )
            )
        ).scalar()

        total = (
            await session.execute(select(func.count(Filing.id)))
        ).scalar()

        top_filers_q = (
            select(Filing.filer_name, func.count(Filing.id).label("cnt"))
            .where(Filing.submit_date_time.startswith(today_str))
            .group_by(Filing.filer_name)
            .order_by(desc("cnt"))
            .limit(10)
        )
        top_filers_result = await session.execute(top_filers_q)
        top_filers = [
            {"name": row[0], "count": row[1]} for row in top_filers_result
        ]

        return {
            "date": today_str,
            "today_total": today_count,
            "today_new_reports": new_reports,
            "today_amendments": amendments,
            "total_in_db": total,
            "top_filers": top_filers,
            "connected_clients": broadcaster.client_count,
            "poll_interval": settings.POLL_INTERVAL,
        }
