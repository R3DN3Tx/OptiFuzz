import asyncio
import threading

import pytest

from backend.events.bus import EventBus

pytestmark = pytest.mark.asyncio


async def test_publish_from_another_thread_is_delivered_to_subscriber() -> None:
    bus = EventBus()
    loop = asyncio.get_running_loop()
    queue = bus.subscribe(loop)

    def _publish_from_thread() -> None:
        bus.publish("job.started", {"job_id": "abc"})

    thread = threading.Thread(target=_publish_from_thread)
    thread.start()
    thread.join()

    event = await asyncio.wait_for(queue.get(), timeout=2)

    assert event.name == "job.started"
    assert event.data == {"job_id": "abc"}


async def test_unsubscribed_queue_receives_nothing_new() -> None:
    bus = EventBus()
    loop = asyncio.get_running_loop()
    queue = bus.subscribe(loop)
    bus.unsubscribe(queue)

    bus.publish("job.started", {"job_id": "abc"})
    await asyncio.sleep(0.05)

    assert queue.empty()


async def test_multiple_subscribers_each_receive_the_event() -> None:
    bus = EventBus()
    loop = asyncio.get_running_loop()
    queue_a = bus.subscribe(loop)
    queue_b = bus.subscribe(loop)

    bus.publish("tree.updated", {"target_id": "t1"})

    event_a = await asyncio.wait_for(queue_a.get(), timeout=2)
    event_b = await asyncio.wait_for(queue_b.get(), timeout=2)
    assert event_a.data == event_b.data == {"target_id": "t1"}
