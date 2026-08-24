import threading

import pytest

from pipeline_queue.event_queue import EventProcessor, EventQueue, QueueCapacityError, QueueClosedError


def test_queue_returns_highest_impact_first_and_tracks_metrics():
    event_queue = EventQueue[str](max_size=3)
    event_queue.put("low", impact_score=2)
    event_queue.put("high", impact_score=9)
    event_queue.put("medium", impact_score=5)

    assert event_queue.get() == "high"
    event_queue.task_done()
    assert event_queue.get() == "medium"
    event_queue.task_done()
    assert event_queue.get() == "low"
    event_queue.task_done()
    assert event_queue.metrics().processed_count == 3


def test_queue_rejects_full_and_closed_submissions():
    event_queue = EventQueue[str](max_size=1)
    event_queue.put("event", impact_score=4)

    with pytest.raises(QueueCapacityError):
        event_queue.put("other", impact_score=4, block=False)

    event_queue.shutdown()
    with pytest.raises(QueueClosedError):
        event_queue.put("late", impact_score=4)


def test_processor_retries_with_exponential_backoff_and_drains_shutdown():
    event_queue = EventQueue[str]()
    attempts = {"count": 0}
    processed = []
    ready = threading.Event()

    def handler(event: str) -> None:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise RuntimeError("temporary failure")
        processed.append(event)
        ready.set()

    processor = EventProcessor(event_queue, handler, max_attempts=3, backoff_seconds=0.001)
    processor.start()
    event_queue.put("event", impact_score=10)

    assert ready.wait(timeout=2)
    event_queue.join()
    processor.stop(timeout=2)

    assert attempts["count"] == 3
    assert processed == ["event"]
    assert event_queue.metrics().processed_count == 1


def test_queue_can_infer_impact_score_from_event_object():
    class ScoredEvent:
        impact_score = 8

    event_queue = EventQueue[ScoredEvent]()
    event = ScoredEvent()
    event_queue.put(event)

    assert event_queue.get() is event
