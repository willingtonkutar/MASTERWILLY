"""Top-level event processing orchestration."""

from __future__ import annotations

import asyncio
import logging
import signal
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from analyzers.claude_analyzer import ClaudeAnalyzer
from filters.keyword_filter import KeywordFilter
from models import NewsEvent
from notifiers.alert_manager import AlertManager
from notifiers.telegram_notifier import TelegramNotifier
from pipeline_queue.event_queue import EventProcessor, EventQueue, QueueCapacityError
from scrapers.forexfactory import ForexFactoryScraper
from scrapers.forexfactory_news import ForexFactoryNewsScraper
from scrapers.twitter_monitor import TwitterMonitor

from .scheduler import SchedulerService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrchestratorMetrics:
    received_events: int
    filtered_events: int
    queued_events: int
    processed_events: int
    failed_events: int
    queue_size: int


class Orchestrator:
    """Coordinate ingestion, filtering, analysis, alerting, and shutdown."""

    def __init__(
        self,
        *,
        event_queue: EventQueue[NewsEvent] | None = None,
        keyword_filter: KeywordFilter | None = None,
        analyzer: ClaudeAnalyzer | None = None,
        alert_manager: AlertManager | None = None,
        forex_factory: ForexFactoryScraper | None = None,
        twitter_monitor: TwitterMonitor | None = None,
        news_monitor: ForexFactoryNewsScraper | None = None,
        scheduler: SchedulerService | None = None,
        enable_forex_news: bool = True,
        news_interval_minutes: int = 5,
        critical_news_interval_minutes: int = 2,
        event_process_window_hours: int = 1,
        worker_count: int = 2,
        executor_workers: int = 4,
    ) -> None:
        if worker_count < 1 or executor_workers < 1:
            raise ValueError("worker_count and executor_workers must be at least 1")
        self.event_queue = event_queue or EventQueue(max_size=1000)
        self.keyword_filter = keyword_filter or KeywordFilter()
        self.analyzer = analyzer or ClaudeAnalyzer()
        self.alert_manager = alert_manager or AlertManager(TelegramNotifier())
        self.forex_factory = forex_factory or ForexFactoryScraper(
            event_queue=self.event_queue,
            seen_state_file="logs/forexfactory_calendar_seen.json",
        )
        self.twitter_monitor = twitter_monitor
        self.news_monitor = news_monitor or ForexFactoryNewsScraper(
            event_queue=self.event_queue,
            seen_state_file="logs/forexfactory_news_seen.json",
        )
        self.enable_forex_news = enable_forex_news
        self._executor = ThreadPoolExecutor(max_workers=executor_workers, thread_name_prefix="pipeline")
        self._processors = [EventProcessor(self.event_queue, self._process_queued_event) for _ in range(worker_count)]
        self.scheduler = scheduler or SchedulerService(
            self.forex_factory,
            self.twitter_monitor,
            news_monitor=self.news_monitor,
            news_check_callback=self.forex_news_check if enable_forex_news else None,
            critical_news_check_callback=self.critical_news_check if enable_forex_news else None,
            news_interval_minutes=news_interval_minutes,
            critical_news_interval_minutes=critical_news_interval_minutes,
            event_process_window_hours=event_process_window_hours,
            health_callback=self.health_check,
        )
        self._metrics_lock = threading.Lock()
        self._metrics = {"received_events": 0, "filtered_events": 0, "queued_events": 0, "processed_events": 0, "failed_events": 0}
        self._stop_event = threading.Event()
        self._signals_installed = False

    def start(self) -> None:
        """Start queue workers and scheduled ingestion."""
        if not self._stop_event.is_set() and any(processor._thread and processor._thread.is_alive() for processor in self._processors):
            raise RuntimeError("orchestrator is already running")
        self._stop_event.clear()
        for processor in self._processors:
            processor.start()
        self.scheduler.start()
        self._install_signal_handlers()
        logger.info("Orchestrator started")

    def submit_event(self, event: NewsEvent, *, block: bool = False, bypass_filter: bool = False, priority: int | None = None) -> bool:
        """Apply the cheap filter and enqueue an event, with backpressure handling."""
        self._increment("received_events")
        match = self.keyword_filter.analyze(event.headline, news_mode=event.source == "forexfactory_news")
        if not match.keywords and not bypass_filter:
            return False
        self._increment("filtered_events")
        normalized = event.model_copy(update={"keywords": match.keywords, "asset_mentions": match.asset_mentions})
        try:
            event_priority = priority or max(1, min(10, round(match.confidence * 10)))
            self.event_queue.put(normalized, impact_score=event_priority, block=block)
        except QueueCapacityError:
            logger.warning("Event queue is full; applying backpressure to %s", event.headline)
            return False
        self._increment("queued_events")
        logger.info("News queued | source=%s priority=%d headline=%s", event.source, event_priority, event.headline)
        return True

    def forex_news_check(self) -> int:
        """Queue unseen Forex Factory news stories matching the news filter."""
        queued = 0
        for event in self.news_monitor.get_breaking_news():
            if self.news_monitor.is_critical(event):
                continue
            if self.news_monitor.claim_new_story(event) and self.submit_event(event):
                queued += 1
        if queued:
            logger.info("Regular news check queued %d new story(s)", queued)
        return queued

    def critical_news_check(self) -> int:
        """Queue unseen critical stories at the highest queue priority."""
        queued = 0
        for event in self.news_monitor.get_critical_news():
            if self.news_monitor.claim_new_story(event) and self.submit_event(event, bypass_filter=True, priority=10):
                queued += 1
        if queued:
            logger.info("Critical news check queued %d high-priority story(s)", queued)
        return queued

    def wait(self) -> None:
        """Block until a shutdown signal or explicit stop."""
        self._stop_event.wait()

    def shutdown(self) -> None:
        """Drain queued events, stop jobs, and release worker resources."""
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        self.scheduler.shutdown(wait=True)
        for processor in self._processors:
            processor.stop(timeout=10)
        self.event_queue.join()
        self._executor.shutdown(wait=True, cancel_futures=False)
        logger.info("Orchestrator shut down")

    def metrics(self) -> OrchestratorMetrics:
        with self._metrics_lock:
            values = dict(self._metrics)
        values["processed_events"] = self.event_queue.metrics().processed_count
        values["queue_size"] = self.event_queue.metrics().queue_size
        return OrchestratorMetrics(**values)

    def health_check(self) -> dict[str, Any]:
        snapshot = self.metrics()
        healthy = snapshot.failed_events == 0 and not self.event_queue.is_shutdown
        result = {"healthy": healthy, "queue_size": snapshot.queue_size, "processed_events": snapshot.processed_events}
        logger.info("Health check: %s", result)
        return result

    def _process_queued_event(self, event: NewsEvent) -> None:
        future = self._executor.submit(self._analyze_and_alert, event)
        try:
            future.result()
            self._increment("processed_events")
        except Exception:
            self._increment("failed_events")
            logger.exception("Event pipeline failed for %s", event.headline)

    def _analyze_and_alert(self, event: NewsEvent) -> None:
        analysis = self.analyzer.analyze_event(event)
        logger.info(
            "Claude analysis | asset=%s sentiment=%s impact=%d action=%s reasoning=%s",
            analysis.asset,
            analysis.sentiment,
            analysis.impact_score,
            analysis.action,
            (analysis.reasoning[:180] + "...") if len(analysis.reasoning) > 180 else analysis.reasoning,
        )
        asyncio.run(self.alert_manager.process_analysis(analysis, event))

    def _install_signal_handlers(self) -> None:
        if self._signals_installed:
            return
        try:
            signal.signal(signal.SIGINT, self._handle_shutdown_signal)
            signal.signal(signal.SIGTERM, self._handle_shutdown_signal)
            self._signals_installed = True
        except (ValueError, OSError):
            logger.debug("Signal handlers can only be installed from the main thread")

    def _handle_shutdown_signal(self, signum: int, frame: Any) -> None:
        logger.info("Shutdown signal received: %s", signum)
        self.shutdown()

    def _increment(self, key: str) -> None:
        with self._metrics_lock:
            self._metrics[key] += 1
