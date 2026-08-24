"""Thread-safe queue primitives for news event processing."""

from .event_queue import EventProcessor, EventQueue, QueueCapacityError, QueueClosedError, QueueMetrics

__all__ = [
    "EventProcessor",
    "EventQueue",
    "QueueCapacityError",
    "QueueClosedError",
    "QueueMetrics",
]
