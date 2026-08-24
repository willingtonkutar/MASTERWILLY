"""Breaking-news scraper for Forex Factory."""

from __future__ import annotations

import itertools
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from models import NewsEvent

logger = logging.getLogger(__name__)


class ForexFactoryNewsScraper:
    """Fetch Forex Factory news blocks and normalize them as news events."""

    DEFAULT_URL = "https://www.forexfactory.com/news"
    DEFAULT_HEADERS = (
        {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"},
        {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/123 Safari/537.36"},
    )
    CRITICAL_TERMS = ("breaking", "alert", "urgent", "just in", "treasury", "fed", "central bank", "intervention")

    def __init__(
        self,
        *,
        url: str = DEFAULT_URL,
        check_interval_seconds: int = 300,
        timeout_seconds: int = 15,
        headers: Iterable[dict[str, str]] | None = None,
        session: requests.Session | None = None,
        event_queue: Any | None = None,
    ) -> None:
        if check_interval_seconds < 1 or timeout_seconds < 1:
            raise ValueError("check interval and timeout must be positive")
        self.url = url
        self.check_interval_seconds = check_interval_seconds
        self.timeout_seconds = timeout_seconds
        self._headers = itertools.cycle(headers or self.DEFAULT_HEADERS)
        self._session = session or requests.Session()
        self._event_queue = event_queue
        self._impact_by_url: dict[str, str] = {}
        self._seen_urls: set[str] = set()
        self._seen_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def get_breaking_news(self) -> list[NewsEvent]:
        """Fetch current Forex Factory news blocks."""
        response = self._session.get(self.url, headers=next(self._headers), timeout=self.timeout_seconds)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        events: list[NewsEvent] = []
        for block in soup.select(".news-block__item"):
            event = self._parse_block(block, response.url)
            if event is not None:
                events.append(event)
        return events

    def get_critical_news(self) -> list[NewsEvent]:
        """Return only stories with a high-impact indicator."""
        return [event for event in self.get_breaking_news() if self._is_critical(event)]

    def impact_level(self, event: NewsEvent) -> str:
        """Return the impact level detected while parsing a story."""
        return "high" if self._is_critical(event) else self._impact_by_url.get(event.url or "", "unknown")

    def is_critical(self, event: NewsEvent) -> bool:
        """Return whether headline/content contains strong critical-news signals."""
        return self._is_critical(event)

    def check_for_updates(self) -> list[NewsEvent]:
        """Fetch unseen stories and enqueue them for pipeline processing."""
        updates = []
        for event in self.get_breaking_news():
            if not event.url:
                continue
            with self._seen_lock:
                if event.url in self._seen_urls:
                    continue
                self._seen_urls.add(event.url)
            updates.append(event)
            if self._event_queue is not None:
                self._event_queue.put(event, impact_score=9 if self._impact_by_url.get(event.url) == "high" else 5)
        return updates

    def claim_new_story(self, event: NewsEvent) -> bool:
        """Mark a story as seen and return whether it should be processed."""
        identity = event.url or event.headline.lower().strip()
        with self._seen_lock:
            if identity in self._seen_urls:
                return False
            self._seen_urls.add(identity)
        return True

    def start(self) -> None:
        """Start periodic news checks in a daemon thread."""
        if self._thread and self._thread.is_alive():
            raise RuntimeError("Forex Factory news scraper is already running")
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._scheduled_loop, name="forexfactory-news", daemon=True)
        self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        """Stop periodic news checks."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout)

    def _scheduled_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.check_for_updates()
            except Exception:
                logger.exception("Scheduled Forex Factory news check failed")
            self._stop_event.wait(self.check_interval_seconds)

    def _parse_block(self, block: Any, response_url: str) -> NewsEvent | None:
        link = block.select_one(".news-block__title a[href]") or block.select_one("a[href*='/news/']")
        if link is None:
            return None
        headline = self._text(link)
        if not headline:
            return None
        url = urljoin(response_url, link.get("href", ""))
        content = self._text(block.select_one(".news-block__preview")) or None
        impact = self._impact_level(block)
        self._impact_by_url[url] = impact
        details = self._text(block.select_one(".news-block__details"))
        timestamp = self._parse_timestamp(details)
        return NewsEvent(
            source="forexfactory_news",
            headline=headline,
            content=content,
            url=url,
            timestamp=timestamp,
            keywords=headline.lower().split(),
            asset_mentions=[],
        )

    def _is_critical(self, event: NewsEvent) -> bool:
        text = f"{event.headline} {event.content or ''}".casefold()
        matches = {term for term in self.CRITICAL_TERMS if term in text}
        return bool(matches & {"breaking", "alert", "urgent", "just in"}) or len(matches) >= 2

    @staticmethod
    def _parse_timestamp(details: str) -> datetime:
        # Forex Factory often exposes relative times such as "16 min ago".
        return datetime.now(timezone.utc)

    @staticmethod
    def _impact_level(block: Any) -> str:
        marker = " ".join(block.get("class", [])) + " " + " ".join(
            " ".join(node.get("class", [])) + " " + node.get("title", "") for node in block.find_all(True)
        )
        marker = marker.lower()
        if "ff-impact-red" in marker or "high impact" in marker:
            return "high"
        if "ff-impact-ora" in marker or "medium impact" in marker:
            return "medium"
        if "ff-impact-yel" in marker or "low impact" in marker:
            return "low"
        return ""

    @staticmethod
    def _text(node: Any) -> str:
        return node.get_text(" ", strip=True) if node is not None else ""
