"""Environment-backed application settings."""

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv

from .errors import ConfigurationError

DEFAULT_KEYWORDS = "iran,war,tariff,bomb,cpi,fed,rates,dxy,gold"


@dataclass(frozen=True)
class Settings:
    app_name: str
    log_level: str
    log_file: Path
    keywords: tuple[str, ...]
    impact_threshold: int
    request_timeout_seconds: int
    anthropic_api_key: str | None
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    forex_news_check_interval: int
    critical_news_check_interval: int
    enable_forex_news: bool
    event_process_window_hours: int
    alert_dedupe_minutes: int
    alert_dedupe_state_file: Path

    @classmethod
    def from_environment(cls, env_file: str | Path | None = ".env") -> "Settings":
        if env_file is not None:
            load_dotenv(env_file)

        keywords = tuple(
            keyword.strip().lower()
            for keyword in os.getenv("KEYWORDS", DEFAULT_KEYWORDS).split(",")
            if keyword.strip()
        )
        if not keywords:
            raise ConfigurationError("KEYWORDS must contain at least one keyword")

        impact_threshold = _read_int("IMPACT_THRESHOLD", 7, minimum=1, maximum=10)
        timeout = _read_int("REQUEST_TIMEOUT_SECONDS", 10, minimum=1)
        forex_news_interval = _read_int("FOREX_NEWS_CHECK_INTERVAL", 5, minimum=1)
        critical_news_interval = _read_int("CRITICAL_NEWS_CHECK_INTERVAL", 2, minimum=1)
        enable_forex_news = _read_bool("ENABLE_FOREX_NEWS", True)
        event_window_hours = _read_int("EVENT_PROCESS_WINDOW_HOURS", 1, minimum=1)
        alert_dedupe_minutes = _read_int("ALERT_DEDUPE_MINUTES", 1440, minimum=0)
        log_level = os.getenv("LOG_LEVEL", "INFO").upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError(f"Unsupported LOG_LEVEL: {log_level}")

        return cls(
            app_name=os.getenv("APP_NAME", "newswithwilly"),
            log_level=log_level,
            log_file=Path(os.getenv("LOG_FILE", "logs/newswithwilly.log")),
            keywords=keywords,
            impact_threshold=impact_threshold,
            request_timeout_seconds=timeout,
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
            forex_news_check_interval=forex_news_interval,
            critical_news_check_interval=critical_news_interval,
            enable_forex_news=enable_forex_news,
            event_process_window_hours=event_window_hours,
            alert_dedupe_minutes=alert_dedupe_minutes,
            alert_dedupe_state_file=Path(os.getenv("ALERT_DEDUPE_STATE_FILE", "logs/alert_dedupe_seen.json")),
        )


def _read_int(name: str, default: int, minimum: int, maximum: int | None = None) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value < minimum or (maximum is not None and value > maximum):
        bound = f"between {minimum} and {maximum}" if maximum else f"at least {minimum}"
        raise ConfigurationError(f"{name} must be {bound}")
    return value


def _read_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name, str(default)).strip().lower()
    if raw_value not in {"true", "false", "1", "0", "yes", "no"}:
        raise ConfigurationError(f"{name} must be a boolean")
    return raw_value in {"true", "1", "yes"}
