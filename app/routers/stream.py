"""Server-Sent Events stream endpoint."""

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.poller import broadcaster

router = APIRouter(tags=["SSE"])


@router.get("/api/stream")
async def sse_stream(request: Request) -> StreamingResponse:
    """Server-Sent Events stream for real-time filing notifications.

    Supports reconnection via the Last-Event-ID header: when a client
    reconnects after a brief disconnect, any events broadcast since its
    last received event ID are replayed immediately.
    """
    # Parse Last-Event-ID for reconnection support
    last_event_id: int | None = None
    raw = request.headers.get("last-event-id")
    if raw:
        try:
            last_event_id = int(raw.strip())
        except (ValueError, TypeError):
            pass

    async def event_generator():
        client_id, queue = await broadcaster.subscribe(last_event_id)
        try:
            yield "retry: 5000\n\n"
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
            await broadcaster.unsubscribe(client_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
