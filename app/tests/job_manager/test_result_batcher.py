import threading
import time

from backend.job_manager.result_batcher import ResultBatcher


def test_rapid_adds_within_interval_are_flushed_together() -> None:
    flushes: list[list[int]] = []
    batcher = ResultBatcher(flush=flushes.append, interval=0.2)

    for i in range(5):
        batcher.add(i)

    time.sleep(0.35)

    assert flushes == [[0, 1, 2, 3, 4]]


def test_flush_now_flushes_immediately_and_cancels_pending_timer() -> None:
    flushes: list[list[int]] = []
    batcher = ResultBatcher(flush=flushes.append, interval=5.0)

    batcher.add(1)
    batcher.add(2)
    batcher.flush_now()

    assert flushes == [[1, 2]]
    # The pending 5s timer was cancelled, so nothing else shows up later.
    time.sleep(0.05)
    assert flushes == [[1, 2]]


def test_flush_now_on_empty_buffer_does_not_call_flush() -> None:
    flushes: list[list[int]] = []
    batcher = ResultBatcher(flush=flushes.append, interval=0.2)

    batcher.flush_now()

    assert flushes == []


def test_multiple_flush_cycles() -> None:
    flushes: list[list[int]] = []
    batcher = ResultBatcher(flush=flushes.append, interval=0.1)

    batcher.add(1)
    time.sleep(0.2)
    batcher.add(2)
    time.sleep(0.2)

    assert flushes == [[1], [2]]


def test_thread_safe_under_concurrent_adds() -> None:
    flushes: list[list[int]] = []
    lock = threading.Lock()

    def flush(items: list[int]) -> None:
        with lock:
            flushes.append(items)

    batcher = ResultBatcher(flush=flush, interval=0.2)

    def _add_many(start: int) -> None:
        for i in range(start, start + 50):
            batcher.add(i)

    threads = [threading.Thread(target=_add_many, args=(i * 50,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    batcher.flush_now()

    all_items = [item for batch in flushes for item in batch]
    assert sorted(all_items) == list(range(200))
