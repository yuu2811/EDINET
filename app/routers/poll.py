"""Manual poll trigger endpoint with rate limiting."""

import asyncio
import logging
import time
from datetime import date as date_type

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Poll"])

_poll_last_called: float = 0.0
_POLL_COOLDOWN = 10.0
_poll_lock = asyncio.Lock()

# Store references to background tasks so they are not garbage-collected mid-execution.
_background_tasks: set[asyncio.Task] = set()


def _on_poll_done(task: asyncio.Task) -> None:
    """Log exceptions from background poll tasks and remove from tracking set."""
    _background_tasks.discard(task)
    if not task.cancelled() and task.exception():
        logger.error("Background poll failed: %s", task.exception(), exc_info=task.exception())


@router.post("/api/poll")
async def trigger_poll(
    date: str | None = Query(None, description="Date to poll (YYYY-MM-DD)"),
) -> dict:
    """Manually trigger an EDINET poll (rate-limited to once per 10s)."""
    global _poll_last_called

    async with _poll_lock:
        now = time.monotonic()
        if now - _poll_last_called < _POLL_COOLDOWN:
            remaining = int(_POLL_COOLDOWN - (now - _poll_last_called))
            return JSONResponse(
                {"error": f"レート制限中です。{remaining}秒後に再試行してください"},
                status_code=429,
            )
        _poll_last_called = now

    from app.poller import poll_edinet

    target_date = None
    if date:
        try:
            target_date = date_type.fromisoformat(date)
        except ValueError:
            return JSONResponse({"error": "無効な日付形式です"}, status_code=400)

    task = asyncio.create_task(poll_edinet(target_date))
    _background_tasks.add(task)
    task.add_done_callback(_on_poll_done)
    return {"status": "poll_triggered", "date": str(target_date or date_type.today())}
