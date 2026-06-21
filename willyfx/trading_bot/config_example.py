"""
Small helper showing how to load runtime settings from a local `.env` file.

This file is a non-executing example and safe to commit. Copy values into
`previous bot/.env` or `previous bot/trading_bot/.env` locally and do NOT
commit secrets.
"""

from dotenv import load_dotenv
import os

# Load .env from repo root and trading_bot folder (mirrors main config.py behavior)
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
load_dotenv(os.path.join(_REPO_ROOT, ".env"))
load_dotenv(os.path.join(_HERE, ".env"))


def get_config():
    return {
        "MT5_LOGIN": os.getenv("MT5_LOGIN"),
        "MT5_PASSWORD": os.getenv("MT5_PASSWORD"),
        "MT5_SERVER": os.getenv("MT5_SERVER"),
        "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN"),
        "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID"),
        "SYMBOL": os.getenv("SYMBOL", "XAUUSD"),
        "TIMEFRAME": os.getenv("TIMEFRAME", "M15"),
    }


if __name__ == "__main__":
    cfg = get_config()
    print("Loaded config preview (secrets hidden):")
    for k, v in cfg.items():
        display = "<hidden>" if "PASSWORD" in k or "TOKEN" in k else v
        print(f"{k}: {display}")
