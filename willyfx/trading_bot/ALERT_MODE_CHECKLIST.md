# Alert Mode Checklist

Steps to run the bot in alert-only (Telegram) mode safely.

1. Create a local `.env` with your secrets (see ENV_INSTRUCTIONS.md).
2. Create a Python virtual environment and install requirements:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

3. Verify Telegram settings in `.env`:
- `TELEGRAM_SIGNAL_BOT_ENABLED=true`
- `TELEGRAM_BOT_TOKEN` set
- `TELEGRAM_CHAT_ID` set

4. Dry-run the startup reporter to confirm formatting and no external calls:

```powershell
python -c "from monitoring.startup_reporter import run_startup_analysis; print(run_startup_analysis(dry_run=True))"
```

5. Start the signal-only bot:

```powershell
python main2.py
```

6. Monitor the logs and Telegram channel for startup message and subsequent alerts.

7. If Telegram messages fail, check `TELEGRAM_BOT_TOKEN` and that the bot is an admin of the channel.

8. After a few days of observing, review `logs/trades.log` and the `state/bot_state.json` (if enabled) to see what setups were posted.

Optional: run `main2.py` under `screen`/`tmux` or a Windows service for continuous running.
