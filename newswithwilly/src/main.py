"""Command-line interface for the NewsWithWilly monitoring system."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path

# Support both ``python -m src.main`` from the repository root and package imports.
SRC_DIR = Path(__file__).resolve().parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from analyzers.claude_analyzer import ClaudeAnalyzer
from core.health import HealthCheck
from core.orchestrator import Orchestrator
from filters.keyword_filter import KeywordFilter
from models import NewsEvent
from notifiers.alert_manager import AlertManager
from notifiers.telegram_notifier import TelegramNotifier
from pipeline_queue.event_queue import EventQueue
from scrapers.forexfactory import ForexFactoryScraper
from newswithwilly.config import Settings
from newswithwilly.logging_config import configure_logging

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="newswithwilly", description="News monitoring trading alert system")
    parser.add_argument("command", choices=("run", "test", "status", "scrape", "analyze"))
    parser.add_argument("headline", nargs="?", help="Headline for the analyze command")
    parser.add_argument("--config", type=Path, help="Custom .env configuration file")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING"), default=None)
    parser.add_argument("--dry-run", action="store_true", help="Do not send Telegram alerts")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    return parser


def load_settings(args: argparse.Namespace) -> Settings:
    settings = Settings.from_environment(env_file=args.config or ".env")
    return replace(settings, log_level=args.log_level or settings.log_level)


def command_run(args: argparse.Namespace, settings: Settings) -> int:
    notifier = TelegramNotifier(bot_token="" if args.dry_run else settings.telegram_bot_token, chat_id="" if args.dry_run else settings.telegram_chat_id)
    orchestrator = Orchestrator(
        keyword_filter=KeywordFilter(settings.keywords),
        analyzer=ClaudeAnalyzer(settings.anthropic_api_key),
        alert_manager=AlertManager(
            notifier,
            impact_threshold=settings.impact_threshold,
            dedupe_minutes=settings.alert_dedupe_minutes,
            seen_state_file=settings.alert_dedupe_state_file,
        ),
    )
    orchestrator.start()
    try:
        if args.once:
            orchestrator.forex_factory.check_once(force_refresh=True)
            orchestrator.event_queue.join()
        else:
            orchestrator.wait()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    finally:
        orchestrator.shutdown()
    return 0


def command_status(settings: Settings) -> int:
    health = HealthCheck(event_queue=EventQueue(max_size=1000))
    report = health.report()
    print(f"STATUS: {report.status}")
    for component in report.components:
        print(f"{component.status:7} {component.name}: {component.message}")
    for name, value in report.metrics.items():
        print(f"{name}: {value}")
    return 0 if report.status != "RED" else 1


def command_scrape(args: argparse.Namespace, settings: Settings) -> int:
    scraper = ForexFactoryScraper(cache_seconds=0)
    events = scraper.check_once(force_refresh=True)
    print(f"Found {len(events)} high-impact USD events")
    for event in events:
        print(f"{event.timestamp.isoformat()} {event.name} ({event.impact_level})")
    return 0


def command_analyze(args: argparse.Namespace, settings: Settings) -> int:
    if not args.headline:
        raise ValueError("analyze requires a headline")
    event = NewsEvent(source="twitter", headline=args.headline, keywords=KeywordFilter(settings.keywords).extract_keywords(args.headline))
    result = ClaudeAnalyzer(settings.anthropic_api_key).analyze_event(event)
    print(result.to_json())
    return 0


def command_test() -> int:
    import pytest

    return pytest.main(["-q"])


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "test":
        return command_test()
    try:
        settings = load_settings(args)
        configure_logging(settings.log_level, settings.log_file)
        if args.command == "run":
            return command_run(args, settings)
        if args.command == "status":
            return command_status(settings)
        if args.command == "scrape":
            return command_scrape(args, settings)
        return command_analyze(args, settings)
    except Exception as exc:
        logger.error("Command failed: %s", exc, exc_info=args.log_level == "DEBUG")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
