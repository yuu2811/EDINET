"""Manual poll trigger endpoint with rate limiting."""

import asyncio
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(tags=["Poll"])

# Simple in-memory rate limiter for poll endpoint
_poll_last_called: float = 0.0
_POLL_COOLDOWN = 10.0  # seconds


@router.post("/api/poll")
async def trigger_poll() -> dict:
    """Manually trigger an EDINET poll (rate-limited to once per 10s)."""
    global _poll_last_called
    now = time.monotonic()
    if now - _poll_last_called < _POLL_COOLDOWN:
        remaining = int(_POLL_COOLDOWN - (now - _poll_last_called))
        return JSONResponse(
            {"error": f"Rate limited. Try again in {remaining}s"},
            status_code=429,
        )
    _poll_last_called = now

    from app.poller import poll_edinet

    asyncio.create_task(poll_edinet())
    return {"status": "poll_triggered"}
