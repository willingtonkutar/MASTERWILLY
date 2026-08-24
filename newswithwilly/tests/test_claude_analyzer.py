from types import SimpleNamespace
from analyzers.claude_analyzer import ClaudeAnalyzer
from models import NewsEvent


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return self.response


def event():
    return NewsEvent(source="twitter", headline="Fed signals higher rates", keywords=["fed", "rates"])


def test_analyzer_parses_structured_response_and_tracks_cost():
    response = SimpleNamespace(
        content=[SimpleNamespace(text='{"asset":"XAUUSD","sentiment":"BEARISH","impact_score":8,"action":"LOOK FOR SELLS","reasoning":"Higher rates pressure gold."}')],
        usage=SimpleNamespace(input_tokens=100, output_tokens=50),
    )
    messages = FakeMessages(response)
    analyzer = ClaudeAnalyzer(client=SimpleNamespace(messages=messages), max_retries=1)

    result = analyzer.analyze_event(event())

    assert result.sentiment == "BEARISH"
    assert result.event_id is not None
    assert messages.calls == 1
    assert analyzer.cost_metrics().input_tokens == 100


def test_analyzer_accepts_json_followed_by_claude_explanation():
    response = SimpleNamespace(
        content=[SimpleNamespace(text='{"asset":"XAUUSD","sentiment":"NEUTRAL","impact_score":4,"action":"HOLD","reasoning":"Limited market impact."}\nThis is a low-confidence result.')]
    )
    analyzer = ClaudeAnalyzer(client=SimpleNamespace(messages=FakeMessages(response)), max_retries=1)

    result = analyzer.analyze_event(event())

    assert result.impact_score == 4
    assert result.reasoning == "Limited market impact."


def test_analyzer_returns_neutral_fallback_without_client():
    result = ClaudeAnalyzer(api_key=None).analyze_event(event())

    assert result.sentiment == "NEUTRAL"
    assert result.impact_score == 1
    assert result.action == "HOLD"


def test_analyzer_falls_back_after_invalid_response():
    response = SimpleNamespace(content=[SimpleNamespace(text="not json")])
    analyzer = ClaudeAnalyzer(client=SimpleNamespace(messages=FakeMessages(response)), max_retries=1)

    result = analyzer.analyze_event(event())

    assert result.sentiment == "NEUTRAL"
    assert result.event_id is not None
