# Institutional Forex Trading Bot

Production-grade XAUUSD trading system with two operational modes:

- `main.py` for live automated trading
- `main2.py` for signal-only Telegram alerts

The project combines deterministic strategy logic, market regime filtering, SMC analysis, Claude-based context validation, centralized logging, and risk controls around execution.

## Overview

The bot is designed to analyze the market first and act second. It loads market data from MT5, calculates indicators, detects regime conditions, evaluates Smart Money Concepts structure, scores the setup, optionally asks Claude for contextual validation, and then either executes a trade or sends the signal to Telegram depending on the entrypoint you run.

`main.py` is the execution version. `main2.py` is the alerting version. Both share the same analysis pipeline up to the point of execution.

## What the system does

- Connects to MetaTrader 5 and reads live market data
- Computes EMA, RSI, MACD, and ATR based structure
- Detects market regime and filters out unfavorable conditions
- Evaluates SMC liquidity, BOS, CHOCH, and related structure cues
- Generates institutional-style BUY/SELL signals
- Uses Claude as a context and validation layer only
- Logs events, errors, and session usage centrally
- Applies safety checks such as spread validation and trade limits
- Supports either live autotrading or Telegram signal delivery

## Entry points

### `main.py`

Live trading mode.

- Runs the full market analysis stack
- Validates the signal
- Calculates entry, stop loss, and take profit
- Sends the order to MT5 when `AUTO_TRADE=True`
- Manages trade registration, exit monitoring, and SL adjustments

### `main2.py`

Signal bot mode.

- Runs the same analysis stack as `main.py`
- Builds the same signal, SL, and TP logic
- Sends a formatted alert to Telegram instead of opening a trade
- Sends a startup message when the bot comes online
- Useful when you want manual review before taking a trade

## Signal flow

1. Fetch market data from MT5
2. Validate candles and indicators
3. Detect regime and skip weak market conditions
4. Evaluate SMC structure and liquidity
5. Generate an institutional score and direction
6. Optionally request Claude context and refinement
7. Confirm spread and setup quality
8. Either execute the trade (`main.py`) or send a Telegram alert (`main2.py`)

## Configuration

All runtime settings are loaded from `.env` through `config.py`.

Important groups of settings include:

- Market and symbol selection
- Risk limits and trade capacity
- Claude routing, token budgets, and prompt caching
- Telegram channel delivery settings
- Signal cooldowns and logging intervals
- Execution validation and state persistence

The repository already ignores `.env`, so credentials stay local to your machine.

## Telegram signal bot

To use the signal-only bot:

1. Set the Telegram environment values in `.env`
2. Make your bot an admin of the destination channel
3. Use the channel ID in `-100xxxxxxxxxx` format
4. Run `python main2.py`

The signal bot sends:

- Direction
- Entry price
- Stop loss
- Take profit
- Claude context summary
- Confluence score and regime information

## Files and responsibilities

- `main.py` - live trading entrypoint
- `main2.py` - Telegram signal entrypoint
- `config.py` - all runtime configuration and environment loading
- `broker/mt5_client.py` - MT5 connectivity and pricing access
- `data/feed.py` - market data and indicator helpers
- `strategy/strategy_engine.py` - signal generation and scoring
- `strategy/smc.py` - Smart Money Concepts analysis
- `strategy/regime_detector.py` - regime classification and filters
- `risk/risk_manager.py` - account and exposure controls
- `execution/engine.py` - trade entry and management
- `execution/exit_engine.py` - exit logic and SL management
- `execution/validation_engine.py` - execution quality checks
- `ai/claude.py` - Claude context analysis, routing, and usage tracking
- `monitoring/logger.py` - centralized event logging
- `monitoring/telegram_notifier.py` - Telegram delivery helper
- `state/state_manager.py` - persistent session and trade state

## Operational behavior

### Trading mode

When you run `main.py`, the bot can:

- open positions
- close positions
- move stop loss to breakeven
- lock profit
- apply trailing stops
- keep trade state across restarts

### Signal mode

When you run `main2.py`, the bot:

- does not open trades
- does not close trades
- does not modify SL or TP on live positions
- sends a polished Telegram notification for valid setups
- can send a startup message for quick connectivity checks

## Logging and monitoring

The bot logs operational events to the console and file logs. This includes:

- startup and shutdown events
- MT5 connection status
- regime changes
- signal detection events
- Telegram delivery success or failure
- Claude usage summary and cost estimates
- trade entry and exit events in live mode

## Safety and risk controls

The code includes guardrails to keep behavior disciplined:

- daily loss limits
- consecutive loss tracking
- maximum open trades
- spread checks before execution
- data validation before analysis
- cooldowns to reduce duplicate signals
- fallback handling for Claude model failures
- state recovery for interrupted sessions

## Recommended use

- Use a demo account first
- Run `main2.py` to review signals before enabling live execution
- Keep Telegram alerts for discretionary review if you prefer manual entries
- Use `main.py` only when you are comfortable with the risk controls and execution behavior

## Setup

```bash
cd trading_bot
pip install -r requirements.txt
```

Then configure your local `.env` file and start one of the entrypoints:

```bash
python main.py
```

or

```bash
python main2.py
```

## Troubleshooting

- If MT5 does not connect, verify the terminal is open and the account is available.
- If Telegram does not send, confirm the bot is an admin of the channel and the chat ID is correct.
- If signals are sparse, that is usually the regime and quality filters doing their job.
- If Claude usage remains at zero, no call path has yet reached a valid Claude validation step during that run.

## Notes

This bot is built for disciplined analysis and controlled execution. It is intentionally structured so the analysis pipeline can be reused for both live trading and manual signal delivery without duplicating the core logic.
