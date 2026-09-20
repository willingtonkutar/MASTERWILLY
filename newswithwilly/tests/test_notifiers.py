import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from models import AnalysisResult, NewsEvent
from models import CalendarEventData
from notifiers.alert_manager import AlertManager
from notifiers.telegram_notifier import TelegramNotifier


class FakeResponse:
    ok = True
    text = "ok"

    def json(self):
        return {"ok": True}


class FakeSession:
    def __init__(self):
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return FakeResponse()


class FakeNotifier:
    def __init__(self):
        self.sent = []

    @staticmethod
    def format_alert_message(analysis, event):
        return f"{analysis.asset}: {event.headline}"

    def send_alert(self, alert, *, silent=False):
        self.sent.append((alert, silent))
        return True


def make_event(headline="Fed raises rates"):
    return NewsEvent(source="forexfactory_news", headline=headline, timestamp=datetime.now(timezone.utc), url="https://example.test/news")


def make_analysis(event, score=9, asset="XAUUSD"):
    return AnalysisResult(event_id=event.id, asset=asset, sentiment="BEARISH", impact_score=score, action="LOOK FOR SELLS", reasoning="Rates pressure gold.", confidence=0.9)


def test_telegram_formats_and_sends_markdown_v2():
    session = FakeSession()
    notifier = TelegramNotifier("token", "chat", thread_id=12, session=session, max_retries=1)
    event = make_event()
    message = notifier.format_alert_message(make_analysis(event), event)
    from models import Alert
    alert = Alert(event_id=event.id, analysis_result_id=event.id, formatted_message=message)

    assert "⚠️" in message
    assert "■" in message
    assert "📉" in message
    assert notifier.send_alert(alert)
    assert session.posts[0][1]["json"]["message_thread_id"] == 12
    assert session.posts[0][1]["json"]["parse_mode"] == "MarkdownV2"


def test_telegram_formats_high_impact_calendar_alert():
    event = NewsEvent(
        source="forexfactory",
        headline="US CPI",
        timestamp=datetime.now(timezone.utc),
        calendar=CalendarEventData(currency="USD", impact_level="high", previous="3.2%", forecast="3.5%"),
    )
    message = TelegramNotifier.format_alert_message(make_analysis(event), event)

    assert r"PRE\-RELEASE PLAN" in message
    assert r"Previous: 3\.2%" in message
    assert "DXY:" not in message
    assert "Claude bias" not in message
    assert "wait for the actual release" in message
    assert r"HIGH\-IMPACT" in message


def test_alert_manager_applies_threshold_and_deduplication():
    notifier = FakeNotifier()
    manager = AlertManager(notifier, impact_threshold=7, dedupe_minutes=30, asset_cooldown_minutes=0)
    event = make_event()

    async def scenario():
        first = await manager.process_analysis(make_analysis(event, score=9), event)
        second = await manager.process_analysis(make_analysis(event, score=8), event)
        low = await manager.process_analysis(make_analysis(make_event("Routine update"), score=6), make_event("Routine update"))
        return first, second, low

    first, second, low = asyncio.run(scenario())
    assert first.sent and first.priority == "HIGH"
    assert not second.sent and "duplicate" in second.reason
    assert low.alert is None and low.priority == "LOW"
    assert len(notifier.sent) == 1


def test_planned_analysis_sends_even_when_claude_score_is_below_threshold():
    notifier = FakeNotifier()
    manager = AlertManager(notifier, impact_threshold=7, asset_cooldown_minutes=0)
    event = make_event("ISM Manufacturing Prices")
    analysis = make_analysis(event, score=6)

    decision = asyncio.run(manager.process_analysis(analysis, event, planned=True))

    assert decision.sent
    assert decision.priority == "LOW"
    assert len(notifier.sent) == 1


def test_planned_analysis_does_not_cool_down_the_actual_alert():
    notifier = FakeNotifier()
    manager = AlertManager(notifier, impact_threshold=7, asset_cooldown_minutes=10)
    planned_event = make_event("ISM Manufacturing Prices")
    actual_event = make_event("ISM Manufacturing Prices actual")

    planned = asyncio.run(manager.process_analysis(make_analysis(planned_event, score=6), planned_event, planned=True))
    actual = asyncio.run(manager.process_analysis(make_analysis(actual_event, score=8), actual_event))

    assert planned.sent
    assert actual.sent


def test_alert_manager_limits_hourly_alerts():
    notifier = FakeNotifier()
    manager = AlertManager(notifier, max_alerts_per_hour=1, dedupe_minutes=0, asset_cooldown_minutes=0)

    async def scenario():
        first_event = make_event("First CPI report")
        second_event = make_event("Second CPI report")
        return (
            await manager.process_analysis(make_analysis(first_event), first_event),
            await manager.process_analysis(make_analysis(second_event), second_event),
        )

    first, second = asyncio.run(scenario())
    assert first.sent
    assert not second.sent
    assert "hourly" in second.reason
