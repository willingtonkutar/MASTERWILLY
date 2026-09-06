# NewsWithWilly

News monitoring and trading alert system foundation. The project is intentionally split into ingestion, analysis, and delivery layers so external providers can be added without coupling them to the application entrypoint.

## Requirements

- Python 3.11 or newer
- Poetry 2.x

## Setup

```powershell
cd newswithwilly
poetry install
Copy-Item .env.example .env
poetry run newswithwilly
```

Keep secrets such as API keys in `.env`. That file is ignored by Git; `.env.example` documents the supported settings without containing credentials.

## Architecture

```text
src/newswithwilly/
	main.py             Application orchestration and CLI error boundary
	config.py           Immutable environment-backed Settings object
	logging_config.py   Console and rotating-file logging configuration
	errors.py           Expected application exception types
src/db/              Async SQLAlchemy models, service, and Alembic environment
src/scrapers/        External source adapters, including Forex Factory
src/filters/         Fast whitelist and asset pre-filtering
src/analyzers/       Structured Claude analysis adapter
tests/                Focused behavior tests
```

The current runtime flow is:

1. **Calendar ingestion** refreshes the Forex Factory weekly calendar and selects medium- and high-impact USD events.
2. **Pre-release scheduling** checks the calendar every five minutes and claims unreleased events entering the next 60-minute window. This produces the planned T-60 alert.
3. **News ingestion** checks Forex Factory breaking news on regular and critical schedules.
4. **Filtering** applies keyword and news-mode rules before paid analysis calls.
5. **Analysis** sends selected events to Claude and validates structured output.
6. **Delivery** sends qualifying alerts to Telegram with deduplication.

## Offline component validation

Use the smoke-test script to verify the core event pipeline without network access, API keys, or Telegram credentials:

```powershell
poetry run python scripts/validate_system.py
```

The script uses the real keyword filter, priority queue, event processor, and alert manager. Claude and Telegram are replaced with deterministic mocks. A successful run prints `[PASS]` for keyword and asset detection, queue processing, analysis, notification, and queue metrics, followed by:

```text
RESULT: all offline component checks passed
```

The process exits with code `0` when every check passes and code `1` when a check fails. This validates local wiring only; use the CLI `scrape`, `analyze`, and `status` commands separately to verify configured external services.
`main.py` currently provides the orchestration boundary and startup health log. Provider adapters should be added as separate modules rather than embedded in the CLI.

### Runtime architecture

```mermaid
flowchart LR
	FF[Forex Factory] --> F[Keyword Filter]
	FFN[Forex Factory News] --> F
	F --> Q[Priority Queue]
	Q --> C[Claude Analyzer]
	C --> AM[Alert Manager]
	AM --> TG[Telegram]
	AM --> DB[(SQLAlchemy Database)]
	S[APScheduler] --> FF
	S --> FFN
	S --> H[Health Monitor]
	H --> LOG[Logs and Status]
```

The database defaults to SQLite through `DATABASE_URL`. For PostgreSQL, set it to an async URL such as `postgresql+asyncpg://user:password@host/database`, then run migrations with `alembic upgrade head`.

The Forex Factory calendar scraper selects medium- and high-impact USD events and processes events within `EVENT_PROCESS_WINDOW_HOURS` of their scheduled time, or past events that have actual data. Adaptive polling uses five minutes normally, 30 seconds near a release, five seconds during the final five minutes before a release, and 30 seconds for 15 minutes afterward. A separate planned-calendar job runs every five minutes and selects unreleased events due within the next 60 minutes. The planned event claim is persisted in `logs/forexfactory_daily_planned_seen.json`, so repeated polling and application restarts do not resend the same T-60 alert.

Calendar events retain their `previous`, `forecast`, `actual`, currency, impact level, release time, and source URL in a typed payload. The payload is persisted in the `news_events.calendar_data` column. Existing databases should run `alembic upgrade head` to apply migration `0002_calendar_event_data`.

`ForexFactoryNewsScraper` reads breaking stories from `https://www.forexfactory.com/news`, deduplicates them by URL, and runs regular and critical checks using `FOREX_NEWS_CHECK_INTERVAL` and `CRITICAL_NEWS_CHECK_INTERVAL`. News mode recognizes policy, central-bank, geopolitical, intervention, bond-purchase, announcement, and surprise-event language. Explicit alert terms or multiple critical terms receive higher queue priority. Set `ENABLE_FOREX_NEWS=false` to disable news monitoring. Claude reads `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, and `CLAUDE_TIMEOUT_SECONDS`.

### Calendar alert behavior

Calendar alerts use a dedicated Telegram layout rather than the generic news layout:

- **High impact:** gold alert header, release countdown, previous and forecast, Claude's preliminary gold bias, surprise map, trading plan, confidence, and a wait-for-actual warning.
- **Medium impact:** shorter gold alert with previous, forecast, directional surprise map, simple plan, confidence, and a wait-for-actual warning.
- **After release:** the actual value is included when Forex Factory publishes it, and the event may be processed again because the actual value changed.

Claude is used for interpretation, not for sourcing numbers. For calendar events it receives the event name, currency, impact, release time, previous, forecast, and actual when available. It returns the preliminary sentiment, action, reasoning, impact score, and confidence. The application owns the countdown, message structure, deduplication, and calendar values.

This first calendar version intentionally does not require live DXY, US10Y, or gold-price data. It does not display fabricated or unavailable market snapshots. A market-data provider can be added later as optional enrichment without changing the calendar scheduling contract.

## Configuration

`Settings.from_environment()` loads `.env` and validates the values needed to start the application. Invalid thresholds, timeouts, log levels, or empty keyword lists raise `ConfigurationError` before monitoring begins.

The monitoring settings are:

```dotenv
FOREX_NEWS_CHECK_INTERVAL=5
CRITICAL_NEWS_CHECK_INTERVAL=2
ENABLE_FOREX_NEWS=true
EVENT_PROCESS_WINDOW_HOURS=1
ALERT_DEDUPE_MINUTES=1440
ALERT_DEDUPE_STATE_FILE=logs/alert_dedupe_seen.json
TIMEZONE_NAME=Africa/Nairobi
```

`ALERT_DEDUPE_MINUTES` controls how long an already-sent story is suppressed across restarts. Set it to `0` to disable alert deduplication.

`TIMEZONE_NAME` controls the calendar's local-day handling and Telegram release-time display. It defaults to `Africa/Nairobi`, so messages display release times in EAT. Calendar timestamps are stored and compared in UTC.

The calendar processes medium- and high-impact USD releases, uses adaptive polling near scheduled releases, persists seen-state across restarts, and reprocesses an event when its actual value changes. Calendar impact controls queue ordering and the high/medium message template; Claude's returned score controls whether a normal queued event sends an alert. Planned calendar analysis is sent regardless of the normal impact threshold so the T-60 message is not silently dropped.

## Logging and errors

Logs go to the console and to a rotating file configured by `LOG_FILE`. Expected operational failures use `NewsWithWillyError` subclasses and return a controlled CLI exit code. Unexpected failures are logged with a traceback and return a different non-zero exit code for process supervisors.

## Tests and quality checks

```powershell
poetry run pytest
poetry run ruff check .
poetry run coverage run -m pytest
poetry run coverage report
```

To verify the live Forex Factory news page without sending an alert:

```powershell
poetry run python scripts/test_forex_news.py --limit 5
```

To run one real Claude-to-Telegram test for the first matching story:

```powershell
poetry run python scripts/test_forex_news.py --send-alert
```

Use `--send-alert` carefully because it makes a Claude API request and sends a real Telegram message. Use `--dry-run` with the main application to disable Telegram delivery.

## CLI

```powershell
python -m src.main run
python -m src.main run --once --dry-run
python -m src.main test
python -m src.main status
python -m src.main scrape
python -m src.main analyze "Fed signals higher rates"
```

Use `--config path\to\custom.env` for an alternate environment file and `--log-level DEBUG` for detailed diagnostics. `--dry-run` disables Telegram delivery.

## Docker deployment

The default Compose setup runs the app with SQLite and named volumes for application data and logs:

```powershell
Copy-Item .env.example .env
docker compose up --build
```

For production, set these values in `.env` before starting the optional PostgreSQL and Redis services:

```dotenv
POSTGRES_PASSWORD=use-a-secret-value
DATABASE_URL=postgresql+asyncpg://newswithwilly:use-a-secret-value@postgres:5432/newswithwilly
SCHEDULER_DATABASE_URL=postgresql://newswithwilly:use-a-secret-value@postgres:5432/newswithwilly
```

Then run:

```powershell
docker compose --profile production up --build
```

The container entrypoint runs `alembic upgrade head` before starting the CLI. Set `RUN_MIGRATIONS=false` only when migrations are managed separately. The process runs as the non-root `app` user, writes logs to the `logs` volume, and uses `exec` so Docker signals reach the Python process for graceful shutdown.

## API reference

### Data models

- `models.CalendarEventData`: typed calendar payload with `currency`, `impact_level`, `previous`, `forecast`, and `actual`.
- `models.NewsEvent`: normalized input with `id`, `source`, `headline`, optional `content` and `url`, `timestamp`, `keywords`, `asset_mentions`, and optional `calendar` data.
- `models.AnalysisResult`: Claude output with `event_id`, `asset`, `sentiment`, `impact_score` from 1 to 10, `action`, `reasoning`, `confidence` from 0 to 1, and `analyzed_at`.
- `models.Alert`: delivery state with `id`, `event_id`, `analysis_result_id`, `formatted_message`, optional `sent_at`, and `status` (`pending`, `sent`, or `failed`).

All Pydantic models expose `to_dict()`, `to_json()`, and `from_dict()`.

### Service methods

| Service | Public methods |
| --- | --- |
| `KeywordFilter` | `pre_filter(text)`, `extract_keywords(text)`, `check_asset_mentions(text)`, `analyze(text)` |
| `ForexFactoryScraper` | `get_weekly_calendar()`, `filter_medium_or_high_impact_usd_events(events)`, `get_due_planned_medium_or_high_impact_usd_events()`, `parse_event_row(row)`, `check_once()` |
| `ForexFactoryNewsScraper` | `get_breaking_news()`, `get_critical_news()`, `check_for_updates()`, `start()`, `stop()` |
| `ClaudeAnalyzer` | `analyze_event(event)`, `cost_metrics()` |
| `TelegramNotifier` | `send_alert(alert)`, `send_batch(alerts)`, `format_alert_message(analysis, event)` |
| `AlertManager` | `process_analysis(analysis, event)` |
| `DatabaseService` | `create_tables()`, `session()`, model CRUD methods, `check_connection()`, `close()` |
| `HealthCheck` | component checks, `report()`, `metrics()` |

Example:

```python
from analyzers.claude_analyzer import ClaudeAnalyzer
from filters.keyword_filter import KeywordFilter
from models import NewsEvent

headline = "Fed signals higher rates"
event = NewsEvent(
	source="forexfactory_news",
	headline=headline,
	keywords=KeywordFilter().extract_keywords(headline),
)
result = ClaudeAnalyzer().analyze_event(event)
print(result.to_json())
```

## Performance tuning

- Keep `KEYWORDS` narrow so irrelevant events stop before network and Claude calls.
- Use the Forex Factory cache to avoid repeated calendar downloads.
- Keep the event queue bounded; a full queue is a backpressure signal, not a reason to silently discard data.
- Increase orchestrator worker counts only when Claude and Telegram rate limits allow more concurrency.
- Keep retry counts and exponential backoff bounded during provider outages.
- Use Telegram pacing and batch delivery carefully to avoid HTTP 429 responses.
- Prefer PostgreSQL for multiple workers or higher write concurrency; SQLite is intended for development.
- Monitor queue backlog, events per minute, average analysis time, API success rate, and alerts per hour.
- Keep logs on a named Docker volume and rotate them through the configured logging handler.

## Troubleshooting

### Poetry or dependency errors

Install Poetry 2.x and run `poetry install` from `newswithwilly`. Confirm the selected interpreter with `poetry run python --version`.

### Telegram alerts do not arrive

Check `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, channel permissions, and `TELEGRAM_THREAD_ID` for topic-based chats. Use `--log-level DEBUG` to inspect delivery and MarkdownV2 errors.

### Claude returns a neutral fallback

Confirm `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, and network access. Neutral fallback is intentional when the API times out, fails validation, or is not configured.

### Database or migration errors

Use `sqlite+aiosqlite` or `postgresql+asyncpg` for `DATABASE_URL`. Use a synchronous PostgreSQL URL without `+asyncpg` for `SCHEDULER_DATABASE_URL`. Confirm PostgreSQL is healthy before `alembic upgrade head`.

### Docker cannot start

Ensure Docker Desktop is running and `.env` exists. `POSTGRES_PASSWORD` is required with `--profile production`. Inspect output with `docker compose logs -f app`.

## Deployment notes

The default Docker Compose deployment uses SQLite with persistent `app_data` and `logs` volumes. The production profile adds PostgreSQL and Redis. Redis is provisioned for future distributed queue work; the current `EventQueue` is intentionally in-process.

The container runs migrations before the application, uses a non-root `app` user, and forwards signals through the shell entrypoint's `exec`. Set `RUN_MIGRATIONS=false` when migrations are handled by a separate release job.
