"""FastAPI application for the EDINET Large Shareholding Monitor."""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import desc, func, or_, select

from app.config import settings
from app.database import async_session, init_db
from app.edinet import edinet_client
from app.models import Filing, Watchlist
from app.poller import broadcaster, run_poller

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup/shutdown lifecycle."""
    await init_db()
    logger.info("Database initialized")

    # Start background poller
    poller_task = asyncio.create_task(run_poller())
    logger.info("Background poller started")

    yield

    # Shutdown
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
# SSE endpoint
# ---------------------------------------------------------------------------


@app.get("/api/stream")
async def sse_stream(request: Request):
    """Server-Sent Events stream for real-time filing notifications."""

    async def event_generator():
        queue = broadcaster.subscribe()
        try:
            # Send initial connection confirmation
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
            try:
                broadcaster.unsubscribe(queue)
            except ValueError:
                pass

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
# Filings API
# ---------------------------------------------------------------------------


@app.get("/api/filings")
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
    # Validate date formats
    if date_from:
        try:
            datetime.strptime(date_from, "%Y-%m-%d")
        except ValueError:
            return JSONResponse(
                {"error": "Invalid date_from format. Use YYYY-MM-DD"},
                status_code=400,
            )
    if date_to:
        try:
            datetime.strptime(date_to, "%Y-%m-%d")
        except ValueError:
            return JSONResponse(
                {"error": "Invalid date_to format. Use YYYY-MM-DD"},
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

        # Get total count
        count_query = select(func.count()).select_from(query.subquery())
        total = (await session.execute(count_query)).scalar()

        # Apply pagination
        result = await session.execute(query.offset(offset).limit(limit))
        filings = result.scalars().all()

        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "filings": [f.to_dict() for f in filings],
        }


@app.get("/api/filings/{doc_id}")
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
# Stats API
# ---------------------------------------------------------------------------


@app.get("/api/stats")
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

        connected = broadcaster.client_count

        return {
            "date": today_str,
            "today_total": today_count,
            "today_new_reports": new_reports,
            "today_amendments": amendments,
            "total_in_db": total,
            "top_filers": top_filers,
            "connected_clients": connected,
            "poll_interval": settings.POLL_INTERVAL,
        }


# ---------------------------------------------------------------------------
# Watchlist API
# ---------------------------------------------------------------------------


@app.get("/api/watchlist")
async def get_watchlist():
    """Get the user's watchlist."""
    async with async_session() as session:
        result = await session.execute(
            select(Watchlist).order_by(Watchlist.created_at)
        )
        items = result.scalars().all()
        return {"watchlist": [w.to_dict() for w in items]}


@app.post("/api/watchlist")
async def add_to_watchlist(request: Request):
    """Add a company to the watchlist."""
    body = await request.json()
    company_name = body.get("company_name", "").strip()
    sec_code = body.get("sec_code", "").strip() or None
    edinet_code = body.get("edinet_code", "").strip() or None

    if not company_name:
        return JSONResponse(
            {"error": "company_name is required"}, status_code=400
        )

    async with async_session() as session:
        item = Watchlist(
            company_name=company_name,
            sec_code=sec_code,
            edinet_code=edinet_code,
        )
        session.add(item)
        await session.commit()
        await session.refresh(item)
        return item.to_dict()


@app.delete("/api/watchlist/{item_id}")
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


@app.get("/api/watchlist/filings")
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


# ---------------------------------------------------------------------------
# Manual poll trigger
# ---------------------------------------------------------------------------


@app.post("/api/poll")
async def trigger_poll():
    """Manually trigger an EDINET poll."""
    from app.poller import poll_edinet

    asyncio.create_task(poll_edinet())
    return {"status": "poll_triggered"}


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
