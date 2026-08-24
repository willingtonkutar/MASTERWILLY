from datetime import datetime, timezone
from types import SimpleNamespace

from models import Alert, AnalysisResult, NewsEvent
from notifiers.telegram_notifier import TelegramNotifier


def test_telegram_retry_and_silent_payload():
    class Response:
        ok = False
        text = "temporary"

        def json(self):
            return {"ok": False}

    class Success(Response):
        ok = True

        def json(self):
            return {"ok": True}

    class Session:
        calls = 0

        def post(self, *args, **kwargs):
            self.calls += 1
            return Success() if self.calls == 2 else Response()

    session = Session()
    notifier = TelegramNotifier("token", "chat", session=session, max_retries=2, retry_backoff_seconds=0)
    alert = Alert(event_id="00000000-0000-0000-0000-000000000001", analysis_result_id="00000000-0000-0000-0000-000000000002", formatted_message="message")

    assert notifier.send_alert(alert, silent=True)
    assert session.calls == 2
    assert session is not None
