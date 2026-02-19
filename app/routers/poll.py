"""Manual poll trigger endpoint with rate limiting."""

import asyncio
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(tags=["Poll"])


@router.post("/api/poll")
async def trigger_poll() -> dict:
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

    asyncio.create_task(poll_edinet())
    return {"status": "poll_triggered"}
