from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Event:
    name: str
    data: dict[str, Any]


class EventBus:
    """Thread-safe pub/sub bridging JobManager's worker threads to the async
    SSE endpoint (docs/ARCHITECTURE.md §6). publish() is called from plain
    threads; subscribers live on the FastAPI event loop, so delivery goes
    through call_soon_threadsafe rather than touching the asyncio.Queue
    directly from another thread.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._subscribers.append((loop, queue))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers = [
                (loop, sub) for loop, sub in self._subscribers if sub is not queue
            ]

    def publish(self, name: str, data: dict[str, Any]) -> None:
        event = Event(name=name, data=data)
        with self._lock:
            subscribers = list(self._subscribers)
        for loop, queue in subscribers:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:
                # Target loop already closed (app shutting down) -- drop it.
                pass


# Single process-wide bus. JobManager and the SSE endpoint share this instance
# (docs/ARCHITECTURE.md §3: "tiny in-process pub/sub bus").
event_bus = EventBus()
