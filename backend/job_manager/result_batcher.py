from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Generic, TypeVar

T = TypeVar("T")


class ResultBatcher(Generic[T]):
    """Coalesces rapid-fire items into periodic flushes (docs/ARCHITECTURE.md
    §6 / spec §55) instead of one event per item -- dirsearch can report
    results faster than any UI usefully renders them.

    A flush is scheduled at most once per `interval`: the first add() after
    an empty buffer arms a timer; every add() before that timer fires just
    appends. flush_now() is for job/run-segment boundaries, so nothing is
    left sitting in the buffer when a job ends.
    """

    def __init__(self, flush: Callable[[list[T]], None], interval: float = 0.25) -> None:
        self._flush = flush
        self._interval = interval
        self._lock = threading.Lock()
        self._buffer: list[T] = []
        self._timer: threading.Timer | None = None

    def add(self, item: T) -> None:
        with self._lock:
            self._buffer.append(item)
            if self._timer is None:
                self._timer = threading.Timer(self._interval, self._on_timer)
                self._timer.daemon = True
                self._timer.start()

    def _on_timer(self) -> None:
        with self._lock:
            items, self._buffer = self._buffer, []
            self._timer = None
        if items:
            self._flush(items)

    def flush_now(self) -> None:
        with self._lock:
            items, self._buffer = self._buffer, []
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        if items:
            self._flush(items)
