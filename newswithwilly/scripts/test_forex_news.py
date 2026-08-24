"""Verify the live Forex Factory news scraper.

Run from the repository directory with:
    .venv\\Scripts\\python.exe scripts\\test_forex_news.py

Add --send-alert to send one opt-in Telegram test alert for the first match.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from filters.keyword_filter import KeywordFilter
from analyzers.claude_analyzer import ClaudeAnalyzer
from newswithwilly.config import Settings
from notifiers.alert_manager import AlertManager
from notifiers.telegram_notifier import TelegramNotifier
from scrapers.forexfactory_news import ForexFactoryNewsScraper


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Test the live Forex Factory news scraper")
    parser.add_argument(
        "--send-alert",
        action="store_true",
        help="Send one test Telegram alert for the first keyword match",
    )
    parser.add_argument("--limit", type=int, default=20, help="Maximum stories to print (default: 20)")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.limit < 1:
        print("--limit must be at least 1", file=sys.stderr)
        return 2

    settings = Settings.from_environment()
    scraper = ForexFactoryNewsScraper(timeout_seconds=settings.request_timeout_seconds)
    keyword_filter = KeywordFilter(settings.keywords)

    print("Fetching Forex Factory breaking news...")
    stories = scraper.get_breaking_news()
    print(f"Fetched {len(stories)} stories")

    matches = []
    for story in stories[: args.limit]:
        impact = scraper.impact_level(story)
        match = keyword_filter.analyze(story.headline, news_mode=True)
        match_text = ", ".join(match.keywords) or "none"
        print(f"[{impact.upper():7}] {story.headline}")
        print(f"          matches: {match_text}")
        print(f"          url: {story.url}")
        if match.keywords:
            matches.append((story, match))

    print(f"Keyword matches: {len(matches)}")
    if not matches:
        print("No matching stories found; no alert sent.")
        return 0

    if args.send_alert:
        story, match = matches[0]
        notifier = TelegramNotifier(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
        )
        analyzer = ClaudeAnalyzer(
            api_key=settings.anthropic_api_key,
            model=None,
            timeout_seconds=settings.request_timeout_seconds,
        )
        print(f"Sending first match to Claude using configured model: {analyzer.model}")
        analysis = analyzer.analyze_event(story)
        print(
            f"Claude result: asset={analysis.asset} sentiment={analysis.sentiment} "
            f"impact={analysis.impact_score}/10 action={analysis.action}"
        )
        print(f"Claude reasoning: {analysis.reasoning}")
        alert_manager = AlertManager(notifier, impact_threshold=1)
        decision = asyncio.run(alert_manager.process_analysis(analysis, story))
        print(f"Test alert sent: {decision.sent} ({decision.reason})")
    else:
        print("Alert sending disabled. Use --send-alert to send one test alert.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Forex Factory news test failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
