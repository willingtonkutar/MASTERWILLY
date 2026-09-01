"""Thresholding, deduplication, and persistence for analysis alerts."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from collections import deque
from pathlib import Path
from typing import Any
from uuid import uuid4

from models import Alert, AnalysisResult, NewsEvent

from .telegram_notifier import TelegramNotifier

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AlertDecision:
    """Outcome metadata for a processed analysis."""

    priority: str
    alert: Alert | None
    sent: bool
    reason: str


class AlertManager:
    """Apply alert policy before handing qualifying events to Telegram."""

    def __init__(
        self,
        notifier: TelegramNotifier,
        database: Any | None = None,
        *,
        impact_threshold: int = 7,
        dedupe_minutes: int = 30,
        max_alerts_per_hour: int = 20,
        asset_cooldown_minutes: int = 10,
        seen_state_file: str | Path | None = None,
        max_seen_items: int = 10_000,
    ) -> None:
        if not 1 <= impact_threshold <= 10:
            raise ValueError("impact_threshold must be between 1 and 10")
        if min(dedupe_minutes, max_alerts_per_hour, asset_cooldown_minutes) < 0:
            raise ValueError("alert policy values cannot be negative")
        if max_seen_items < 1:
            raise ValueError("max_seen_items must be positive")
        self.notifier = notifier
        self.database = database
        self.impact_threshold = impact_threshold
        self.dedupe_window = timedelta(minutes=dedupe_minutes)
        self.max_alerts_per_hour = max_alerts_per_hour
        self.asset_cooldown = timedelta(minutes=asset_cooldown_minutes)
        self._seen_state_file = Path(seen_state_file) if seen_state_file else None
        self._max_seen_items = max_seen_items
        self._recent_headlines: dict[str, datetime] = self._load_recent_seen()
        self._asset_sent_at: dict[str, datetime] = {}
        self._sent_times: deque[datetime] = deque()

    async def process_analysis(self, analysis: AnalysisResult, event: NewsEvent) -> AlertDecision:
        """Evaluate, persist, and deliver an analysis according to alert policy."""
        priority = self._priority(analysis.impact_score)
        if priority == "LOW":
            logger.info("Low-impact event logged without alert: %s", event.headline)
            return AlertDecision(priority, None, False, "impact score below threshold")

        now = datetime.now(timezone.utc)
        dedupe_keys = self._dedupe_keys(event)
        if self._is_duplicate(dedupe_keys, now) or await self._is_persisted_duplicate(event, now):
            return AlertDecision(priority, None, False, "duplicate headline within dedupe window")
        if self._is_asset_cooling_down(analysis.asset, now):
            return AlertDecision(priority, None, False, "asset cooldown is active")
        if not self._within_hourly_limit(now):
            return AlertDecision(priority, None, False, "hourly alert limit reached")

        alert = Alert(
            event_id=event.id,
            analysis_result_id=self._analysis_id(analysis),
            formatted_message=self.notifier.format_alert_message(analysis, event),
        )
        if self.database is not None:
            await self.database.create_alert(alert.model_dump(mode="json"))
        sent = self.notifier.send_alert(alert, silent=priority != "HIGH")
        sent_at = datetime.now(timezone.utc) if sent else None
        if self.database is not None:
            await self.database.update_alert(alert.id, {"status": "sent" if sent else "failed", "sent_at": sent_at})
        if sent:
            for key in dedupe_keys:
                self._recent_headlines[key] = now
            self._asset_sent_at[analysis.asset.upper()] = now
            self._sent_times.append(now)
            self._persist_recent_seen(now)
            logger.info(
                "Telegram alert sent | priority=%s asset=%s impact=%d headline=%s",
                priority,
                analysis.asset,
                analysis.impact_score,
                event.headline,
            )
            alert = alert.model_copy(update={"status": "sent", "sent_at": sent_at})
            return AlertDecision(priority, alert, True, "alert sent")
        logger.error(
            "Telegram alert failed | priority=%s asset=%s impact=%d headline=%s",
            priority,
            analysis.asset,
            analysis.impact_score,
            event.headline,
        )
        alert = alert.model_copy(update={"status": "failed"})
        return AlertDecision(priority, alert, False, "Telegram delivery failed")

    def _priority(self, impact_score: int) -> str:
        if impact_score >= 9:
            return "HIGH"
        if impact_score >= self.impact_threshold:
            return "MEDIUM"
        return "LOW"

    def _is_duplicate(self, keys: list[str], now: datetime) -> bool:
        for key in keys:
            previous = self._recent_headlines.get(key)
            if previous is not None and now - previous <= self.dedupe_window:
                return True
        return False

    async def _is_persisted_duplicate(self, event: NewsEvent, now: datetime) -> bool:
        if self.database is None:
            return False
        for alert in await self.database.list_alerts():
            if alert.status != "sent" or alert.event_id == str(event.id) or alert.sent_at is None:
                continue
            previous_event = await self.database.get_news_event(alert.event_id)
            if previous_event and self._headline_key(previous_event.headline) == self._headline_key(event.headline):
                previous_time = alert.sent_at
                if previous_time.tzinfo is None:
                    previous_time = previous_time.replace(tzinfo=timezone.utc)
                if now - previous_time <= self.dedupe_window:
                    return True
        return False

    def _is_asset_cooling_down(self, asset: str, now: datetime) -> bool:
        if self.asset_cooldown <= timedelta(0):
            return False
        previous = self._asset_sent_at.get(asset.upper())
        return previous is not None and now - previous <= self.asset_cooldown

    def _within_hourly_limit(self, now: datetime) -> bool:
        cutoff = now - timedelta(hours=1)
        while self._sent_times and self._sent_times[0] < cutoff:
            self._sent_times.popleft()
        return len(self._sent_times) < self.max_alerts_per_hour

    @staticmethod
    def _headline_key(headline: str) -> str:
        return " ".join(headline.casefold().split())

    @classmethod
    def _dedupe_keys(cls, event: NewsEvent) -> list[str]:
        keys = [f"headline:{cls._headline_key(event.headline)}"]
        if event.url:
            keys.append(f"url:{event.url.strip()}")
        return keys

    def _load_recent_seen(self) -> dict[str, datetime]:
        if self._seen_state_file is None or not self._seen_state_file.exists():
            return {}
        try:
            payload = json.loads(self._seen_state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("Failed to load alert dedupe state from %s", self._seen_state_file)
            return {}
        if not isinstance(payload, dict):
            return {}
        seen: dict[str, datetime] = {}
        for key, value in payload.items():
            if not key or not isinstance(value, str):
                continue
            try:
                stamp = datetime.fromisoformat(value)
            except ValueError:
                continue
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            seen[str(key)] = stamp
        self._prune_recent_seen(datetime.now(timezone.utc), seen)
        return seen

    def _persist_recent_seen(self, now: datetime) -> None:
        if self._seen_state_file is None:
            return
        self._prune_recent_seen(now, self._recent_headlines)
        try:
            self._seen_state_file.parent.mkdir(parents=True, exist_ok=True)
            items = sorted(self._recent_headlines.items(), key=lambda item: item[1])
            if len(items) > self._max_seen_items:
                items = items[-self._max_seen_items :]
                self._recent_headlines = dict(items)
            payload = {key: value.isoformat() for key, value in items}
            self._seen_state_file.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            logger.warning("Failed to persist alert dedupe state to %s", self._seen_state_file)

    def _prune_recent_seen(self, now: datetime, seen: dict[str, datetime]) -> None:
        if self.dedupe_window <= timedelta(0):
            seen.clear()
            return
        cutoff = now - self.dedupe_window
        stale = [key for key, stamp in seen.items() if stamp < cutoff]
        for key in stale:
            seen.pop(key, None)

    @staticmethod
    def _analysis_id(analysis: AnalysisResult) -> str:
        return str(getattr(analysis, "id", None) or uuid4())
