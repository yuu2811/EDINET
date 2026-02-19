"""Server-Sent Events stream endpoint."""

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.poller import broadcaster

router = APIRouter(tags=["SSE"])


@router.get("/api/stream")
async def sse_stream(request: Request) -> StreamingResponse:
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
