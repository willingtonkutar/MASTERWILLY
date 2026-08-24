"""Offline smoke test for the NewsWithWilly event-to-alert pipeline.

Run with: poetry run python scripts/validate_system.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from filters.keyword_filter import KeywordFilter
from models import AnalysisResult, NewsEvent
from notifiers.alert_manager import AlertManager
from pipeline_queue.event_queue import EventProcessor, EventQueue


class MockAnalyzer:
    def analyze_event(self, event: NewsEvent) -> AnalysisResult:
        return AnalysisResult(
            event_id=event.id,
            asset="XAUUSD",
            sentiment="BULLISH",
            impact_score=9,
            action="LOOK FOR BUYS",
            reasoning="Mock safe-haven demand supports gold.",
            confidence=0.9,
        )


class MockNotifier:
    def __init__(self) -> None:
        self.sent_messages: list[str] = []

    @staticmethod
    def format_alert_message(analysis: AnalysisResult, event: NewsEvent) -> str:
        return f"HIGH IMPACT | {analysis.asset} | {event.headline}"

    def send_alert(self, alert, *, silent: bool = False) -> bool:
        self.sent_messages.append(alert.formatted_message)
        return True


def main() -> int:
    print("NewsWithWilly offline validation")
    keyword_filter = KeywordFilter()
    headline = "Fed signals higher rates as Iran tensions lift gold"
    event = NewsEvent(
        source="twitter",
        headline=headline,
        timestamp=datetime.now(timezone.utc),
    )

    match = keyword_filter.analyze(event.headline)
    if not match.keywords or "XAUUSD" not in match.asset_mentions:
        raise AssertionError("keyword filter did not detect expected terms/assets")
    event = event.model_copy(update={"keywords": match.keywords, "asset_mentions": match.asset_mentions})
    print(f"[PASS] filter matched: {', '.join(match.keywords)}")
    print(f"[PASS] assets detected: {', '.join(match.asset_mentions)}")

    event_queue: EventQueue[NewsEvent] = EventQueue(max_size=10)
    notifier = MockNotifier()
    manager = AlertManager(
        notifier,
        impact_threshold=7,
        dedupe_minutes=30,
        asset_cooldown_minutes=0,
    )
    analyzer = MockAnalyzer()
    processed = []

    def process(event_to_process: NewsEvent) -> None:
        analysis = analyzer.analyze_event(event_to_process)
        decision = asyncio.run(manager.process_analysis(analysis, event_to_process))
        if not decision.sent:
            raise AssertionError(f"alert was not sent: {decision.reason}")
        processed.append(decision)

    processor = EventProcessor(event_queue, process, max_attempts=1)
    processor.start()
    event_queue.put(event, impact_score=9)
    event_queue.join()
    processor.stop(timeout=2)

    if len(processed) != 1 or len(notifier.sent_messages) != 1:
        raise AssertionError("queue-to-Telegram pipeline did not process exactly one alert")
    metrics = event_queue.metrics()
    print("[PASS] event queued and processed")
    print("[PASS] mock Claude analysis returned impact score 9")
    print(f"[PASS] mock Telegram accepted: {notifier.sent_messages[0]}")
    print(f"[PASS] queue metrics: size={metrics.queue_size}, processed={metrics.processed_count}")
    print("RESULT: all offline component checks passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"RESULT: validation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
