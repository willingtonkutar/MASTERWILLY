"""Thread-safe priority queue and retrying event processor."""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

logger = logging.getLogger(__name__)
EventT = TypeVar("EventT")


class QueueClosedError(RuntimeError):
    """Raised when an event is submitted after queue shutdown begins."""


class QueueCapacityError(RuntimeError):
    """Raised when a non-blocking submission finds the queue at capacity."""


@dataclass(frozen=True)
class QueueMetrics:
    """A point-in-time snapshot of queue activity."""

    queue_size: int
    processed_count: int


class EventQueue(Generic[EventT]):
    """A bounded, thread-safe priority queue with graceful draining shutdown."""

    def __init__(self, max_size: int = 1000) -> None:
        if max_size < 1:
            raise ValueError("max_size must be at least 1")
        self._queue: queue.PriorityQueue[tuple[int, int, EventT]] = queue.PriorityQueue(maxsize=max_size)
        self._max_size = max_size
        self._lock = threading.Lock()
        self._sequence = 0
        self._processed_count = 0
        self._closed = False

    @property
    def max_size(self) -> int:
        return self._max_size

    def put(self, event: EventT, impact_score: int | None = None, *, block: bool = True, timeout: float | None = None) -> None:
        """Add an event, with higher impact scores dequeued first."""
        priority = self._resolve_priority(event, impact_score)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                if self._closed:
                    raise QueueClosedError("event queue is shut down")
                sequence = self._sequence
                self._sequence += 1
            try:
                self._queue.put((-priority, sequence, event), block=False)
                return
            except queue.Full as exc:
                if not block:
                    raise QueueCapacityError("event queue is full") from exc
                if deadline is not None and time.monotonic() >= deadline:
                    raise QueueCapacityError("event queue is full") from exc
                time.sleep(0.01)

    def get(self, *, block: bool = True, timeout: float | None = None) -> EventT | None:
        """Get the next event, or None once shutdown has drained the queue."""
        remaining = timeout
        while True:
            try:
                item = self._queue.get(block=False)
                return item[2]
            except queue.Empty:
                with self._lock:
                    closed = self._closed
                if closed:
                    return None
                if not block:
                    raise
                if remaining is not None:
                    if remaining <= 0:
                        raise
                    wait_for = min(remaining, 0.1)
                    remaining -= wait_for
                else:
                    wait_for = 0.1
                time.sleep(wait_for)

    def task_done(self) -> None:
        """Mark a retrieved event as processed."""
        self._queue.task_done()
        with self._lock:
            self._processed_count += 1

    def join(self) -> None:
        """Wait until all submitted events are completed."""
        self._queue.join()

    def shutdown(self) -> None:
        """Stop new submissions and let consumers drain existing events."""
        with self._lock:
            self._closed = True

    @property
    def is_shutdown(self) -> bool:
        with self._lock:
            return self._closed

    def metrics(self) -> QueueMetrics:
        with self._lock:
            return QueueMetrics(self._queue.qsize(), self._processed_count)

    @staticmethod
    def _resolve_priority(event: EventT, impact_score: int | None) -> int:
        value = impact_score if impact_score is not None else getattr(event, "impact_score", 0)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10:
            raise ValueError("impact_score must be an integer from 1 to 10")
        return value


class EventProcessor(Generic[EventT]):
    """Background consumer that retries failed handlers with exponential backoff."""

    def __init__(self, event_queue: EventQueue[EventT], handler: Callable[[EventT], None], *, max_attempts: int = 3, backoff_seconds: float = 1.0) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if backoff_seconds < 0:
            raise ValueError("backoff_seconds cannot be negative")
        self._queue = event_queue
        self._handler = handler
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("event processor is already running")
        self._thread = threading.Thread(target=self._run, name="event-processor", daemon=True)
        self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        self._queue.shutdown()
        if self._thread is not None:
            self._thread.join(timeout)

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        while True:
            event = self._queue.get()
            if event is None:
                return
            started_at = time.perf_counter()
            try:
                self._process_with_retries(event)
            finally:
                self._queue.task_done()
                logger.info("Processed event in %.3fs", time.perf_counter() - started_at)

    def _process_with_retries(self, event: EventT) -> None:
        for attempt in range(1, self._max_attempts + 1):
            try:
                self._handler(event)
                return
            except Exception:
                logger.exception("Event processing failed on attempt %d/%d", attempt, self._max_attempts)
                if attempt == self._max_attempts:
                    logger.error("Event discarded after %d attempts", self._max_attempts)
                    return
                delay = self._backoff_seconds * (2 ** (attempt - 1))
                if delay:
                    time.sleep(delay)
