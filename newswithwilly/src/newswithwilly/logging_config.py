"""Central logging configuration for console and rotating file output."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

_CONSOLE_INFO_NAMESPACES = (
    "newswithwilly",
    "core.orchestrator",
    "scrapers.forexfactory",
    "scrapers.forexfactory_news",
    "notifiers.alert_manager",
    "notifiers.telegram_notifier",
)


class _ConsoleNoiseFilter(logging.Filter):
    """Keep console output focused on business events and problems."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        if record.levelno < logging.INFO:
            return False
        return record.name.startswith(_CONSOLE_INFO_NAMESPACES)


def configure_logging(level: str, log_file: Path) -> None:
    """Configure predictable console and file handlers once at application startup."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(LOG_FORMAT)
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    if root_logger.handlers:
        return

    console_handler = logging.StreamHandler()
    console_handler.addFilter(_ConsoleNoiseFilter())
    console_handler.setFormatter(formatter)
    file_handler = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)
