"""Forex Factory calendar scraper with caching, retries, and queue delivery."""

from __future__ import annotations

import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as time_type, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterable
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from models import NewsEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ForexFactoryEvent:
    """Calendar event retaining Forex Factory's economic values."""

    name: str
    timestamp: datetime
    currency: str
    impact_level: str
    previous: str | None = None
    forecast: str | None = None
    actual: str | None = None
    url: str | None = None
    id: UUID = field(default_factory=uuid4)
    source: str = "forexfactory"

    @property
    def impact_score(self) -> int:
        return {"high": 9, "medium": 5, "low": 2}.get(self.impact_level.lower(), 1)

    def to_news_event(self) -> NewsEvent:
        values = [self.name, self.currency, self.impact_level]
        content = " | ".join(
            f"{label}: {value}" for label, value in zip(("Previous", "Forecast", "Actual"), (self.previous, self.forecast, self.actual)) if value
        )
        return NewsEvent(
            id=self.id,
            source="forexfactory",
            headline=self.name,
            content=content or None,
            url=self.url,
            timestamp=self.timestamp,
            keywords=[value.lower() for value in values if value],
            asset_mentions=["XAUUSD", "DXY"],
        )


class ForexFactoryScraper:
    """Fetch and normalize the weekly calendar without redundant requests."""

    DEFAULT_URL = "https://www.forexfactory.com/calendar.php?week=this"
    DEFAULT_HEADERS = (
        {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"},
        {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/123 Safari/537.36"},
        {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17 Safari/605.1.15"},
    )

    def __init__(
        self,
        *,
        url: str = DEFAULT_URL,
        timezone_name: str = "UTC",
        cache_seconds: int = 300,
        check_interval_seconds: int = 300,
        max_retries: int = 3,
        backoff_seconds: float = 1.0,
        timeout_seconds: int = 15,
        headers: Iterable[dict[str, str]] | None = None,
        session: requests.Session | None = None,
        event_queue: Any | None = None,
    ) -> None:
        if cache_seconds < 0 or check_interval_seconds < 1 or max_retries < 1 or timeout_seconds < 1:
            raise ValueError("cache/check intervals, retries, and timeout must be positive")
        try:
            self.local_timezone = ZoneInfo(timezone_name)
        except Exception as exc:
            raise ValueError(f"Unknown timezone: {timezone_name}") from exc
        self.url = url
        self.cache_seconds = cache_seconds
        self.check_interval_seconds = check_interval_seconds
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.timeout_seconds = timeout_seconds
        self._headers = itertools.cycle(headers or self.DEFAULT_HEADERS)
        self._session = session or requests.Session()
        self._event_queue = event_queue
        self._cache: tuple[float, list[ForexFactoryEvent]] | None = None
        self._cache_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._scheduler_thread: threading.Thread | None = None
        self.seen_events: dict[str, str | None] = {}

    def get_weekly_calendar(self, *, force_refresh: bool = False) -> list[ForexFactoryEvent]:
        """Fetch, parse, and cache this week's calendar."""
        now = time.monotonic()
        with self._cache_lock:
            if self._cache and not force_refresh and now - self._cache[0] < self.cache_seconds:
                return list(self._cache[1])

        response = self._request_with_retries()
        events = self._parse_calendar(response.text, response.url)
        with self._cache_lock:
            self._cache = (time.monotonic(), events)
        return list(events)

    def filter_high_impact_usd_events(self, events: Iterable[ForexFactoryEvent]) -> list[ForexFactoryEvent]:
        """Keep only USD events marked high impact."""
        return [event for event in events if event.currency.upper() == "USD" and event.impact_level.lower() == "high"]

    def check_once(self, *, force_refresh: bool = False, process_window_hours: int = 1) -> list[ForexFactoryEvent]:
        """Fetch high-impact USD events and enqueue them when configured."""
        events = self.filter_high_impact_usd_events(self.get_weekly_calendar(force_refresh=force_refresh))
        now = datetime.now(timezone.utc)
        window = timedelta(hours=process_window_hours)
        events = [
            event for event in events
            if (now <= event.timestamp <= now + window) or (event.timestamp < now and event.actual)
        ]
        new_events = []
        for event in events:
            event_key = str(event.id)
            signature = event.actual
            if self.seen_events.get(event_key, object()) == signature:
                continue
            self.seen_events[event_key] = signature
            new_events.append(event)
            if self._event_queue is not None:
                try:
                    self._event_queue.put(event.to_news_event(), impact_score=event.impact_score)
                except Exception:
                    logger.exception("Failed to enqueue Forex Factory event: %s", event.name)
        return new_events

    def start_scheduled_checks(self) -> None:
        """Start periodic checks in a daemon thread."""
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            raise RuntimeError("scheduled checks are already running")
        self._stop_event.clear()
        self._scheduler_thread = threading.Thread(target=self._scheduled_loop, name="forexfactory-scraper", daemon=True)
        self._scheduler_thread.start()

    def stop_scheduled_checks(self, timeout: float | None = None) -> None:
        self._stop_event.set()
        if self._scheduler_thread:
            self._scheduler_thread.join(timeout)

    def start_adaptive_checks(self, *, process_window_hours: int = 1) -> None:
        """Poll normally, then accelerate around scheduled high-impact events."""
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            raise RuntimeError("scheduled checks are already running")
        self._stop_event.clear()
        self._scheduler_thread = threading.Thread(
            target=self._adaptive_loop,
            args=(process_window_hours,),
            name="forexfactory-adaptive",
            daemon=True,
        )
        self._scheduler_thread.start()

    def _adaptive_loop(self, process_window_hours: int) -> None:
        while not self._stop_event.is_set():
            try:
                events = self.filter_high_impact_usd_events(self.get_weekly_calendar(force_refresh=True))
                self.check_once(process_window_hours=process_window_hours)
                now = datetime.now(timezone.utc)
                upcoming = [event.timestamp - now for event in events if event.timestamp >= now]
                recent = [now - event.timestamp for event in events if timedelta(0) <= now - event.timestamp <= timedelta(minutes=15)]
                nearest = min(upcoming, default=timedelta.max)
                if nearest <= timedelta(minutes=5):
                    interval = 5
                elif recent or nearest <= timedelta(minutes=30):
                    interval = 30
                else:
                    interval = self.check_interval_seconds
            except Exception:
                logger.exception("Adaptive Forex Factory check failed")
                interval = self.check_interval_seconds
            self._stop_event.wait(interval)

    def parse_event_row(self, row: Any, *, reference_date: date | None = None) -> ForexFactoryEvent | None:
        """Parse a calendar table row, returning None for incomplete rows."""
        try:
            currency = self._text(row.select_one(".calendar__currency"))
            name = self._text(row.select_one(".calendar__event"))
            impact_node = row.select_one(".calendar__impact")
            impact_level = self._impact_level(impact_node)
            if not currency or not name or not impact_level:
                return None
            event_date = self._parse_date(row.get("data-date"), reference_date)
            event_time = self._text(row.select_one(".calendar__time"))
            timestamp = self._parse_timestamp(event_date, event_time)
            values = [self._text(row.select_one(f".calendar__{field}")) or None for field in ("previous", "forecast", "actual")]
            external_id = row.get("data-event-id") or f"{currency}:{name}:{timestamp.isoformat()}"
            event_id = uuid5(NAMESPACE_URL, f"forexfactory:{external_id}")
            return ForexFactoryEvent(name=name, timestamp=timestamp, currency=currency, impact_level=impact_level, previous=values[0], forecast=values[1], actual=values[2], url=row.get("data-url"), id=event_id)
        except (TypeError, ValueError, OverflowError):
            logger.exception("Failed to parse Forex Factory event row")
            return None

    def _request_with_retries(self) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self._session.get(self.url, headers=next(self._headers), timeout=self.timeout_seconds)
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                last_error = exc
                logger.warning("Forex Factory request failed (%d/%d): %s", attempt, self.max_retries, exc)
                if attempt < self.max_retries:
                    time.sleep(self.backoff_seconds * (2 ** (attempt - 1)))
        raise RuntimeError("Forex Factory calendar unavailable") from last_error

    def _parse_calendar(self, html: str, response_url: str | None) -> list[ForexFactoryEvent]:
        soup = BeautifulSoup(html, "html.parser")
        reference_date = datetime.now(self.local_timezone).date()
        events = []
        for row in soup.select("tr.calendar__row"):
            event = self.parse_event_row(row, reference_date=reference_date)
            if event is not None:
                events.append(event)
        return events

    def _scheduled_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.check_once()
            except Exception:
                logger.exception("Scheduled Forex Factory check failed")
            self._stop_event.wait(self.check_interval_seconds)

    def _parse_timestamp(self, event_date: date, event_time: str) -> datetime:
        normalized_time = event_time.lower()
        if not event_time or normalized_time in {"all day", "tentative"} or normalized_time.startswith("day "):
            local_value = datetime.combine(event_date, time_type.min)
        else:
            parsed_time = datetime.strptime(event_time.upper(), "%I:%M%p").time()
            local_value = datetime.combine(event_date, parsed_time)
        return local_value.replace(tzinfo=self.local_timezone).astimezone(timezone.utc)

    @staticmethod
    def _parse_date(raw_date: str | None, fallback: date | None) -> date:
        if raw_date:
            try:
                return parsedate_to_datetime(raw_date).date()
            except (TypeError, ValueError):
                return datetime.fromisoformat(raw_date).date()
        if fallback is None:
            raise ValueError("event row has no date")
        return fallback

    @staticmethod
    def _text(node: Any) -> str:
        return node.get_text(" ", strip=True) if node is not None else ""

    @staticmethod
    def _impact_level(node: Any) -> str:
        if node is None:
            return ""
        nodes = [node, *node.find_all(True)]
        for candidate in nodes:
            classes = " ".join(candidate.get("class", []))
            title = candidate.get("title", "")
            marker = f"{classes} {title}".lower()
            for marker_name, level in (("ff-impact-red", "high"), ("ff-impact-ora", "medium"), ("ff-impact-yel", "low")):
                if marker_name in marker:
                    return level
            for level in ("high", "medium", "low"):
                if level in marker:
                    return level
        return ""
