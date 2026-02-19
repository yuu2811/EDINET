"""Manual poll trigger endpoint with rate limiting."""

import asyncio
import time
from datetime import date as date_type

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

router = APIRouter(tags=["Poll"])


@router.post("/api/poll")
async def trigger_poll(
    date: str | None = Query(None, description="Date to poll (YYYY-MM-DD)"),
) -> dict:
    """Manually trigger an EDINET poll (rate-limited to once per 10s)."""
    import app.main as _main

    now = time.monotonic()
    if now - _main._poll_last_called < _main._POLL_COOLDOWN:
        remaining = int(_main._POLL_COOLDOWN - (now - _main._poll_last_called))
        return JSONResponse(
            {"error": f"Rate limited. Try again in {remaining}s"},
            status_code=429,
        )
    _main._poll_last_called = now

    from app.poller import poll_edinet

    target_date = None
    if date:
        try:
            target_date = date_type.fromisoformat(date)
        except ValueError:
            return JSONResponse({"error": "Invalid date format"}, status_code=400)

    asyncio.create_task(poll_edinet(target_date))
    return {"status": "poll_triggered", "date": str(target_date or date_type.today())}
