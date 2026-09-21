from datetime import datetime, timezone
from types import SimpleNamespace
from analyzers.claude_analyzer import ClaudeAnalyzer
from models import CalendarEventData, NewsEvent


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.calls = 0
        self.kwargs = []

    def create(self, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
        return self.response


def event():
    return NewsEvent(source="forexfactory_news", headline="Fed signals higher rates", keywords=["fed", "rates"])


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
    assert messages.kwargs[0]["max_tokens"] == 1000


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


def test_analyzer_rejects_nested_response_fields_without_unhashable_error():
    response = SimpleNamespace(
        content=[SimpleNamespace(text='{"asset":"XAUUSD","sentiment":{"value":"BEARISH"},"impact_score":8,"action":"LOOK FOR SELLS","reasoning":"Higher rates pressure gold."}')]
    )
    analyzer = ClaudeAnalyzer(client=SimpleNamespace(messages=FakeMessages(response)), max_retries=1)

    result = analyzer.analyze_event(event())

    assert result.sentiment == "NEUTRAL"
    assert "failed after retries" in result.reasoning


def test_news_prompt_checks_safe_haven_and_oil_offsets_for_xauusd():
    news_event = NewsEvent(
        source="forexfactory_news",
        headline="Iran attacks raise oil prices",
        content="Crude futures moved higher after the announcement.",
        keywords=["iran", "oil rises"],
        asset_mentions=["XAUUSD"],
    )

    prompt = ClaudeAnalyzer._prompt_for(news_event)

    assert "safe-haven demand" in prompt
    assert "oil or crude-price upside" in prompt
    assert "Do not assume that geopolitical escalation is automatically bullish for gold" in prompt
    assert "Crude futures moved higher" in prompt
    assert "XAUUSD" in prompt
    assert "OIL: likely direction and mechanism" in prompt
    assert "DXY: likely direction and mechanism" in prompt
    assert "GOLD: safe-haven effect versus oil/USD/yield offset" in prompt
    assert "SUMMARY: immediate XAUUSD bias" in prompt


def test_news_prompt_keeps_cross_asset_guidance_for_geopolitical_terms_only():
    regular_prompt = ClaudeAnalyzer._prompt_for(
        NewsEvent(source="forexfactory_news", headline="Company announces new product", keywords=["announces"])
    )
    geopolitical_prompt = ClaudeAnalyzer._prompt_for(
        NewsEvent(source="forexfactory_news", headline="US bombing Iran", keywords=["iran", "bomb"])
    )

    assert "Do not infer unrelated oil or geopolitical effects" in regular_prompt
    assert "OIL: likely direction and mechanism" not in regular_prompt
    assert "OIL: likely direction and mechanism" in geopolitical_prompt


def test_calendar_analysis_prompt_includes_structured_values():
    calendar_event = NewsEvent(
        source="forexfactory",
        headline="Consumer Price Index",
        timestamp=datetime.now(timezone.utc),
        calendar=CalendarEventData(currency="USD", impact_level="high", previous="3.2%", forecast="3.5%"),
    )
    prompt = ClaudeAnalyzer._prompt_for(calendar_event)

    assert "Previous: 3.2%" in prompt
    assert "Forecast: 3.5%" in prompt
    assert "calendar-only" in prompt
    assert "ABOVE FORECAST" in prompt
    assert "BELOW FORECAST" in prompt
    assert "Do not use labels such as \"Claude bias\"" in prompt
