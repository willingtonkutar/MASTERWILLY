from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from scrapers.forexfactory import ForexFactoryEvent, ForexFactoryScraper


HTML = """
<table>
<tr class="calendar__row" data-date="2026-08-24T12:00:00+00:00">
  <td class="calendar__currency">USD</td>
  <td class="calendar__impact"><span title="High Impact"></span></td>
  <td class="calendar__time">8:30am</td>
  <td class="calendar__event">Consumer Price Index</td>
  <td class="calendar__previous">3.0%</td>
  <td class="calendar__forecast">3.1%</td>
  <td class="calendar__actual"></td>
</tr>
<tr class="calendar__row" data-date="2026-08-24T14:00:00+00:00">
  <td class="calendar__currency">EUR</td>
  <td class="calendar__impact"><span title="High Impact"></span></td>
  <td class="calendar__time">10:00am</td>
  <td class="calendar__event">ECB Rate Decision</td>
</tr>
</table>
"""


class FakeResponse:
    url = "https://example.test/calendar"
    text = HTML

    def raise_for_status(self):
        return None


class FakeSession:
    def __init__(self):
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        return FakeResponse()


def test_scraper_parses_filters_and_caches_calendar():
    session = FakeSession()
    scraper = ForexFactoryScraper(session=session, timezone_name="UTC", cache_seconds=300)

    events = scraper.get_weekly_calendar()
    scraper.get_weekly_calendar()
    selected = scraper.filter_high_impact_usd_events(events)

    assert session.calls == 1
    assert len(events) == 2
    assert len(selected) == 1
    assert selected[0].name == "Consumer Price Index"
    assert selected[0].forecast == "3.1%"
    assert selected[0].impact_score == 9
    assert selected[0].timestamp.hour == 8


def test_parse_event_row_converts_local_time_to_utc():
    scraper = ForexFactoryScraper(timezone_name="America/New_York")
    from bs4 import BeautifulSoup

    row = BeautifulSoup(HTML, "html.parser").select_one("tr.calendar__row")
    event = scraper.parse_event_row(row, reference_date=date(2026, 8, 24))

    assert event is not None
    assert event.timestamp.hour == 12
    assert event.to_news_event().source == "forexfactory"


def test_check_once_deduplicates_events_and_reprocesses_actual_updates():
    class Queue:
        def __init__(self):
            self.events = []

        def put(self, event, *, impact_score):
            self.events.append(event)

    queue = Queue()
    scraper = ForexFactoryScraper(event_queue=queue)
    calendar_event = ForexFactoryEvent(
        name="CPI", timestamp=datetime.now(timezone.utc) + timedelta(minutes=10), currency="USD", impact_level="high", id=uuid4()
    )
    scraper.get_weekly_calendar = lambda **kwargs: [calendar_event]

    assert len(scraper.check_once()) == 1
    assert len(scraper.check_once()) == 0
    scraper.get_weekly_calendar = lambda **kwargs: [replace(calendar_event, actual="3.1%")]
    assert len(scraper.check_once()) == 1
    assert len(queue.events) == 2
