"""Telegram MarkdownV2 notification delivery."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterable
from datetime import datetime
from typing import Any

import requests

from models import Alert, AnalysisResult, NewsEvent

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Send formatted alerts through Telegram's Bot API."""

    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: str | None = None,
        *,
        thread_id: int | None = None,
        max_retries: int = 3,
        retry_backoff_seconds: float = 1.0,
        min_interval_seconds: float = 0.0,
        timeout_seconds: float = 10.0,
        session: requests.Session | None = None,
    ) -> None:
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN") if bot_token is None else bot_token
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID") if chat_id is None else chat_id
        configured_thread = thread_id if thread_id is not None else os.getenv("TELEGRAM_THREAD_ID")
        self.thread_id = int(configured_thread) if configured_thread else None
        self.max_retries = max(1, max_retries)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self.min_interval_seconds = max(0.0, min_interval_seconds)
        self.timeout_seconds = timeout_seconds
        self._session = session or requests.Session()
        self._last_send_at = 0.0

    def send_alert(self, alert: Alert, *, silent: bool = False) -> bool:
        """Send an alert with retries; return False for configuration or API failures."""
        if not self.bot_token or not self.chat_id:
            logger.error("Telegram credentials are not configured")
            return False
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": alert.formatted_message,
            "parse_mode": "MarkdownV2",
            "disable_notification": silent,
        }
        if self.thread_id is not None:
            payload["message_thread_id"] = self.thread_id
        return self._post(payload)

    def send_batch(self, alerts: Iterable[Alert], *, silent: bool = False) -> int:
        """Send alerts sequentially and return the number accepted by Telegram."""
        return sum(self.send_alert(alert, silent=silent) for alert in alerts)

    @staticmethod
    def format_alert_message(analysis: AnalysisResult, event: NewsEvent) -> str:
        """Format an analysis as Telegram MarkdownV2."""
        score = max(1, min(10, analysis.impact_score))
        bar = "■" * score + "□" * (10 - score)
        sentiment_icon = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "🟡"}[analysis.sentiment]
        action_upper = analysis.action.upper()
        action_icon = "🚀" if "BUY" in action_upper else "📉" if "SELL" in action_upper else "⏸️"
        header = "⚠️ *HIGH IMPACT ALERT*" if score >= 9 else "*MARKET NEWS ALERT*"
        source = f"[Open source]({TelegramNotifier._escape_url(event.url)})" if event.url else TelegramNotifier._escape("Source: " + event.source)
        return (
            f"{header}\n\n"
            f"*Asset:* {sentiment_icon} {TelegramNotifier._escape(analysis.asset)}\n"
            f"*Sentiment:* {sentiment_icon} {TelegramNotifier._escape(analysis.sentiment)}\n"
            f"*Impact:* `{bar}` {score}/10\n"
            f"*Action:* {action_icon} {TelegramNotifier._escape(analysis.action)}\n\n"
            f"*Headline:* {TelegramNotifier._escape(event.headline)}\n"
            f"*Reasoning:* {TelegramNotifier._escape(analysis.reasoning)}\n"
            f"*Confidence:* {analysis.confidence:.0%}\n"
            f"{source}"
        )

    def _post(self, payload: dict[str, Any]) -> bool:
        for attempt in range(1, self.max_retries + 1):
            elapsed = time.monotonic() - self._last_send_at
            if elapsed < self.min_interval_seconds:
                time.sleep(self.min_interval_seconds - elapsed)
            try:
                response = self._session.post(
                    f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                    json=payload,
                    timeout=self.timeout_seconds,
                )
                if response.ok and response.json().get("ok", True):
                    self._last_send_at = time.monotonic()
                    return True
                logger.warning("Telegram rejected alert (%d/%d): %s", attempt, self.max_retries, response.text[:200])
            except requests.RequestException as exc:
                logger.warning("Telegram request failed (%d/%d): %s", attempt, self.max_retries, exc)
            if attempt < self.max_retries:
                time.sleep(self.retry_backoff_seconds * (2 ** (attempt - 1)))
        return False

    @staticmethod
    def _escape(value: str) -> str:
        reserved = r"_[]()~`>#+-=|{}.!"
        return "".join("\\" + char if char in reserved else char for char in str(value))

    @classmethod
    def _escape_url(cls, url: str) -> str:
        return url.replace("\\", "\\\\").replace(")", "\\)").replace("(", "\\(")
