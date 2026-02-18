"""FastAPI application for the EDINET Large Shareholding Monitor."""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import desc, func, or_, select

from app.config import settings
from app.database import async_session, init_db
from app.edinet import edinet_client
from app.models import Filing, Watchlist
from app.poller import broadcaster, run_poller
from app.schemas import WatchlistCreate

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup/shutdown lifecycle."""
    await init_db()
    logger.info("Database initialized")

    poller_task = asyncio.create_task(run_poller())
    logger.info("Background poller started")

    yield

    poller_task.cancel()
    try:
        await poller_task
    except asyncio.CancelledError:
        pass
    await edinet_client.close()
    logger.info("Shutdown complete")


app = FastAPI(
    title="EDINET 大量保有モニター",
    description="Real-time monitoring of large shareholding reports from EDINET",
    version="1.0.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory="static"), name="static")


# ---------------------------------------------------------------------------
# Router: SSE
# ---------------------------------------------------------------------------

sse_router = APIRouter(tags=["SSE"])


@sse_router.get("/api/stream")
async def sse_stream(request: Request):
    """Server-Sent Events stream for real-time filing notifications."""

    async def event_generator():
        queue = broadcaster.subscribe()
        try:
            yield "event: connected\ndata: {\"status\": \"connected\"}\n\n"

            while True:
                if await request.is_disconnected():
                    break

                try:
                    message = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield message
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            broadcaster.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Router: Filings
# ---------------------------------------------------------------------------

filings_router = APIRouter(prefix="/api/filings", tags=["Filings"])


@filings_router.get("")
async def list_filings(
    date_from: str | None = Query(None, description="Start date (YYYY-MM-DD)"),
    date_to: str | None = Query(None, description="End date (YYYY-MM-DD)"),
    filer: str | None = Query(None, description="Filer name search"),
    target: str | None = Query(None, description="Target company name search"),
    sec_code: str | None = Query(None, description="Securities code"),
    amendment_only: bool = Query(False, description="Show only amendments"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """List large shareholding filings with filters."""
    for label, value in [("date_from", date_from), ("date_to", date_to)]:
        if value:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                return JSONResponse(
                    {"error": f"Invalid {label} format. Use YYYY-MM-DD"},
                    status_code=400,
                )

    async with async_session() as session:
        query = select(Filing).order_by(desc(Filing.submit_date_time), desc(Filing.id))

        if date_from:
            query = query.where(Filing.submit_date_time >= date_from)
        if date_to:
            query = query.where(Filing.submit_date_time <= date_to + " 23:59")
        if filer:
            query = query.where(
                or_(
                    Filing.filer_name.contains(filer),
                    Filing.holder_name.contains(filer),
                )
            )
        if target:
            query = query.where(
                or_(
                    Filing.target_company_name.contains(target),
                    Filing.doc_description.contains(target),
                )
            )
        if sec_code:
            query = query.where(
                or_(
                    Filing.sec_code == sec_code,
                    Filing.target_sec_code == sec_code,
                )
            )
        if amendment_only:
            query = query.where(Filing.is_amendment.is_(True))

        count_query = select(func.count()).select_from(query.subquery())
        total = (await session.execute(count_query)).scalar()

        result = await session.execute(query.offset(offset).limit(limit))
        filings = result.scalars().all()

        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "filings": [f.to_dict() for f in filings],
        }


@filings_router.get("/{doc_id}")
async def get_filing(doc_id: str):
    """Get a single filing by document ID."""
    async with async_session() as session:
        result = await session.execute(
            select(Filing).where(Filing.doc_id == doc_id)
        )
        filing = result.scalar_one_or_none()
        if not filing:
            return JSONResponse({"error": "Filing not found"}, status_code=404)
        return filing.to_dict()


# ---------------------------------------------------------------------------
# Router: Stats
# ---------------------------------------------------------------------------

stats_router = APIRouter(tags=["Stats"])


@stats_router.get("/api/stats")
async def get_stats():
    """Get statistics for the dashboard."""
    today = date.today()
    today_str = today.strftime("%Y-%m-%d")

    async with async_session() as session:
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


# ---------------------------------------------------------------------------
# Router: Watchlist
# ---------------------------------------------------------------------------

watchlist_router = APIRouter(prefix="/api/watchlist", tags=["Watchlist"])


@watchlist_router.get("")
async def get_watchlist():
    """Get the user's watchlist."""
    async with async_session() as session:
        result = await session.execute(
            select(Watchlist).order_by(Watchlist.created_at)
        )
        items = result.scalars().all()
        return {"watchlist": [w.to_dict() for w in items]}


@watchlist_router.post("")
async def add_to_watchlist(body: WatchlistCreate):
    """Add a company to the watchlist."""
    async with async_session() as session:
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
@watchlist_router.get("/filings")
async def get_watchlist_filings():
    """Get recent filings matching the watchlist."""
    async with async_session() as session:
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
            .order_by(desc(Filing.submit_date_time))
            .limit(50)
        )
        result = await session.execute(query)
        filings = result.scalars().all()

        return {"filings": [f.to_dict() for f in filings]}


@watchlist_router.delete("/{item_id}")
async def remove_from_watchlist(item_id: int):
    """Remove a company from the watchlist."""
    async with async_session() as session:
        result = await session.execute(
            select(Watchlist).where(Watchlist.id == item_id)
        )
        item = result.scalar_one_or_none()
        if not item:
            return JSONResponse({"error": "Not found"}, status_code=404)
        await session.delete(item)
        await session.commit()
        return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Router: Poll
# ---------------------------------------------------------------------------

poll_router = APIRouter(tags=["Poll"])


@poll_router.post("/api/poll")
async def trigger_poll():
    """Manually trigger an EDINET poll."""
    from app.poller import poll_edinet

    asyncio.create_task(poll_edinet())
    return {"status": "poll_triggered"}


# ---------------------------------------------------------------------------
# Register routers
# ---------------------------------------------------------------------------

app.include_router(sse_router)
app.include_router(filings_router)
app.include_router(stats_router)
app.include_router(watchlist_router)
app.include_router(poll_router)


# ---------------------------------------------------------------------------
# HTML entry point
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the main dashboard."""
    try:
        with open("static/index.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(
            content="<h1>Dashboard not found</h1><p>static/index.html is missing</p>",
            status_code=500,
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=True,
    )
