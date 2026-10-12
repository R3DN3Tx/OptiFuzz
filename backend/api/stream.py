from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from starlette.responses import StreamingResponse

from backend.events.bus import event_bus

router = APIRouter(tags=["stream"])

KEEPALIVE_SECONDS = 15


@router.get("/api/stream")
async def stream(request: Request) -> StreamingResponse:
    """One multiplexed SSE stream for every job/tree/note event, tagged by
    id in each event's payload (docs/ARCHITECTURE.md §6). The frontend opens
    a single EventSource and filters client-side rather than one connection
    per job.
    """
    loop = asyncio.get_running_loop()
    queue = event_bus.subscribe(loop)

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield f"event: {event.name}\ndata: {json.dumps(event.data)}\n\n"
        finally:
            event_bus.unsubscribe(queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
