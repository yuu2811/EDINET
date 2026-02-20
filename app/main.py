"""FastAPI application for the EDINET Large Shareholding Monitor."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import async_session, init_db  # noqa: F401 – routers resolve async_session here
from app.edinet import edinet_client
from app.errors import register_error_handlers
from app.poller import run_poller
from app.routers import analytics, filings, poll, stats, stock, stream, watchlist

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

_poll_last_called: float = 0.0  # rate limiter state for /api/poll
_POLL_COOLDOWN = 10.0


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
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)
register_error_handlers(app)
for r in (stream.router, filings.router, filings.documents_router, stats.router, watchlist.router, poll.router, stock.router, analytics.router):
    app.include_router(r)
app.mount("/static", StaticFiles(directory="static"), name="static")


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
    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT, reload=True)
