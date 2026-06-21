# ============================================================
#  TELEGRAM NOTIFIER - Signal Delivery Helper
# ============================================================

import html
import requests

import config
from monitoring.logger import log_event


class TelegramNotifier:
    """Class wrapper for Telegram notifications."""
    def __init__(self, config):
        self.config = config
    
    def send_message(self, message):
        """Send a message to Telegram."""
        return send_telegram_signal(message)


def telegram_is_configured():
    return bool(
        config.TELEGRAM_SIGNAL_BOT_ENABLED
        and config.TELEGRAM_BOT_TOKEN
        and config.TELEGRAM_CHAT_ID
    )


def escape_telegram_html(text):
    if text is None:
        return ""
    return html.escape(str(text), quote=False)


def _chat_id_candidates(raw_chat_id):
    """Return chat id variants to improve delivery for Telegram channels."""
    chat_id = str(raw_chat_id).strip()
    candidates = [chat_id]

    if chat_id.isdigit() and not chat_id.startswith("-100"):
        candidates.append(f"-100{chat_id}")

    return candidates


def send_telegram_signal(message):
    """Send a formatted signal message to a Telegram channel or chat."""

    if not config.TELEGRAM_SIGNAL_BOT_ENABLED:
        log_event("TELEGRAM_DISABLED", {"message": "Telegram signal bot disabled by config"})
        return False

    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        log_event("TELEGRAM_NOT_CONFIGURED", {
            "message": "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"
        })
        return False

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    candidate_chat_ids = _chat_id_candidates(config.TELEGRAM_CHAT_ID)

    try:
        last_error = None

        for candidate_chat_id in candidate_chat_ids:
            payload = {
                "chat_id": candidate_chat_id,
                "text": message,
                "parse_mode": config.TELEGRAM_PARSE_MODE,
                "disable_web_page_preview": config.TELEGRAM_DISABLE_WEB_PAGE_PREVIEW,
            }

            response = requests.post(url, data=payload, timeout=15)
            if response.status_code == 200:
                log_event("TELEGRAM_SIGNAL_SENT", {
                    "chat_id": candidate_chat_id,
                    "parse_mode": config.TELEGRAM_PARSE_MODE
                })
                return True

            last_error = {
                "status_code": response.status_code,
                "response": response.text[:300],
                "chat_id": candidate_chat_id,
            }

        log_event("TELEGRAM_SEND_FAILED", {
            "status_code": last_error.get("status_code") if last_error else "unknown",
            "response": last_error.get("response") if last_error else "unknown",
            "attempted_chat_ids": ", ".join(candidate_chat_ids)
        })
        return False

    except requests.RequestException as exc:
        log_event("TELEGRAM_SEND_ERROR", {"error": str(exc)})
        return False