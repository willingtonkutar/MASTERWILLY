"""Component health checks and runtime metrics."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ComponentHealth:
    name: str
    status: str
    message: str
    checked_at: datetime


@dataclass(frozen=True)
class HealthReport:
    status: str
    components: tuple[ComponentHealth, ...]
    metrics: dict[str, float | int]


class HealthCheck:
    """Check component availability and maintain lightweight process metrics."""

    def __init__(self, *, database: Any | None = None, forex_factory: Any | None = None, twitter: Any | None = None, claude: Any | None = None, telegram: Any | None = None, event_queue: Any | None = None) -> None:
        self.database = database
        self.forex_factory = forex_factory
        self.twitter = twitter
        self.claude = claude
        self.telegram = telegram
        self.event_queue = event_queue
        self._lock = threading.Lock()
        self._started_at = time.monotonic()
        self._processed_events = 0
        self._analysis_durations: list[float] = []
        self._api_calls: dict[str, list[int]] = {}
        self._alerts_sent: list[float] = []

    def check_database_connection(self) -> ComponentHealth:
        if self.database is None:
            return self._result("database", "YELLOW", "database service is not configured")
        try:
            result = self.database.check_connection()
            self._resolve(result)
            return self._result("database", "GREEN", "connection available")
        except Exception as exc:
            return self._result("database", "RED", str(exc))

    def check_forexfactory_scraper(self) -> ComponentHealth:
        return self._check_object("forexfactory", self.forex_factory, "scraper configured")

    def check_twitter_api(self) -> ComponentHealth:
        if self.twitter is None:
            return self._result("twitter", "YELLOW", "Twitter monitor is not configured")
        configured = bool(getattr(self.twitter, "bearer_token", None) or getattr(self.twitter, "_client", None))
        return self._result("twitter", "GREEN" if configured else "YELLOW", "credentials/client configured" if configured else "credentials missing")

    def check_claude_api(self) -> ComponentHealth:
        if self.claude is None:
            return self._result("claude", "YELLOW", "Claude analyzer is not configured")
        configured = bool(getattr(self.claude, "api_key", None) or getattr(self.claude, "_client", None))
        return self._result("claude", "GREEN" if configured else "YELLOW", "client configured" if configured else "API key missing")

    def check_telegram_api(self) -> ComponentHealth:
        if self.telegram is None:
            return self._result("telegram", "YELLOW", "Telegram notifier is not configured")
        configured = bool(getattr(self.telegram, "bot_token", None) and getattr(self.telegram, "chat_id", None))
        return self._result("telegram", "GREEN" if configured else "YELLOW", "credentials configured" if configured else "credentials missing")

    def check_queue_status(self) -> ComponentHealth:
        if self.event_queue is None:
            return self._result("queue", "RED", "event queue is not configured")
        try:
            metrics = self.event_queue.metrics()
            backlog = metrics.queue_size
            status = "RED" if self.event_queue.is_shutdown else "GREEN"
            return self._result("queue", status, f"backlog={backlog}")
        except Exception as exc:
            return self._result("queue", "RED", str(exc))

    def report(self) -> HealthReport:
        checks = (
            self.check_database_connection(),
            self.check_forexfactory_scraper(),
            self.check_twitter_api(),
            self.check_claude_api(),
            self.check_telegram_api(),
            self.check_queue_status(),
        )
        status = "RED" if any(item.status == "RED" for item in checks) else "YELLOW" if any(item.status == "YELLOW" for item in checks) else "GREEN"
        report = HealthReport(status, checks, self.metrics())
        logger.info("Health status=%s components=%s metrics=%s", report.status, [(item.name, item.status) for item in checks], report.metrics)
        return report

    def record_event_processed(self) -> None:
        with self._lock:
            self._processed_events += 1

    def record_analysis(self, duration_seconds: float, success: bool) -> None:
        with self._lock:
            self._analysis_durations.append(max(0.0, duration_seconds))
            self._record_api_call("claude", success)

    def record_api_call(self, service: str, success: bool) -> None:
        with self._lock:
            self._record_api_call(service, success)

    def record_alert_sent(self) -> None:
        with self._lock:
            self._alerts_sent.append(time.monotonic())

    def metrics(self) -> dict[str, float | int]:
        now = time.monotonic()
        with self._lock:
            elapsed_minutes = max((now - self._started_at) / 60.0, 1 / 60)
            cutoff = now - 3600
            self._alerts_sent = [stamp for stamp in self._alerts_sent if stamp >= cutoff]
            processed = self._processed_events
            average = sum(self._analysis_durations) / len(self._analysis_durations) if self._analysis_durations else 0.0
            success_rates = [sum(values) / len(values) for values in self._api_calls.values() if values]
            api_success = sum(success_rates) / len(success_rates) if success_rates else 0.0
        backlog = self.event_queue.metrics().queue_size if self.event_queue is not None else 0
        return {
            "events_processed_per_minute": round(processed / elapsed_minutes, 3),
            "average_analysis_time_seconds": round(average, 4),
            "api_success_rate": round(api_success, 3),
            "queue_backlog": backlog,
            "alerts_sent_per_hour": len(self._alerts_sent),
        }

    def _check_object(self, name: str, component: Any | None, message: str) -> ComponentHealth:
        if component is None:
            return self._result(name, "YELLOW", f"{name} is not configured")
        return self._result(name, "GREEN", message)

    @staticmethod
    def _resolve(value: Any) -> Any:
        return asyncio.run(value) if asyncio.iscoroutine(value) else value

    @staticmethod
    def _record_api_call_unlocked(calls: dict[str, list[int]], service: str, success: bool) -> None:
        calls.setdefault(service, []).append(int(success))

    def _record_api_call(self, service: str, success: bool) -> None:
        self._record_api_call_unlocked(self._api_calls, service, success)

    @staticmethod
    def _result(name: str, status: str, message: str) -> ComponentHealth:
        return ComponentHealth(name, status, message, datetime.now(timezone.utc))
