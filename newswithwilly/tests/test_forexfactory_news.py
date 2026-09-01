import requests

from scrapers.forexfactory_news import ForexFactoryNewsScraper


HTML = """
<div class="news-block__item">
  <div class="news-block__title"><a href="/news/123-gold-rises">Gold rises after Fed comments</a></div>
  <div class="news-block__details">10 min ago</div>
  <div class="news-block__preview">Markets reacted to the latest central bank comments.</div>
  <span class="icon icon--ff-impact-red"></span>
</div>
<div class="news-block__item">
  <div class="news-block__title"><a href="/news/124-dollar-steady">Dollar steady ahead of data</a></div>
  <div class="news-block__details">20 min ago</div>
  <div class="news-block__preview">Traders are waiting for the economic release.</div>
</div>
"""


class FakeResponse:
    url = "https://www.forexfactory.com/news"
    text = HTML

    def raise_for_status(self):
        return None


class FakeSession:
    def __init__(self):
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        return FakeResponse()


def test_scraper_parses_news_and_filters_critical_stories():
    scraper = ForexFactoryNewsScraper(session=FakeSession())

    stories = scraper.get_breaking_news()
    critical = scraper.get_critical_news()

    assert len(stories) == 2
    assert stories[0].source == "forexfactory_news"
    assert stories[0].content.startswith("Markets reacted")
    assert stories[0].url.endswith("/news/123-gold-rises")
    assert len(critical) == 1
    assert critical[0].headline == "Gold rises after Fed comments"


def test_get_breaking_news_retries_after_transient_connection_error():
    session = FailOnceSession()
    scraper = ForexFactoryNewsScraper(session=session)

    stories = scraper.get_breaking_news()

    assert len(stories) == 2
    assert session.calls == 2


def test_check_for_updates_deduplicates_and_enqueues():
    queue = FakeQueue()
    scraper = ForexFactoryNewsScraper(session=FakeSession(), event_queue=queue)

    assert len(scraper.check_for_updates()) == 2
    assert len(scraper.check_for_updates()) == 0
    assert len(queue.events) == 2
    assert queue.scores == [9, 5]


class FailOnceSession:
    def __init__(self):
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise requests.exceptions.ConnectionError("Remote end closed connection without response")
        return FakeResponse()


class FakeQueue:
    def __init__(self):
        self.events = []
        self.scores = []

    def put(self, event, *, impact_score):
        self.events.append(event)
        self.scores.append(impact_score)
