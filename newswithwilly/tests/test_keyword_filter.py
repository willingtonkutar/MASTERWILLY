import pytest

from filters.keyword_filter import KeywordFilter


def test_filter_matches_boundaries_and_aliases():
    keyword_filter = KeywordFilter()

    assert keyword_filter.pre_filter("The FED held rates steady and gold rallied")
    assert keyword_filter.extract_keywords("The FED held rates steady and gold rallied") == ["fed", "rates", "gold"]
    assert keyword_filter.check_asset_mentions("Gold and the US dollar moved higher") == ["XAUUSD", "DXY"]
    assert not keyword_filter.pre_filter("golden retriever story")


def test_filter_detects_negation_and_scores_matches():
    result = KeywordFilter().analyze("No war expected, but CPI surprises markets")

    assert result.keywords == ["war", "cpi"]
    assert result.negated_keywords == ["war"]
    assert 0 < result.confidence <= 1


def test_filter_news_mode_matches_unscheduled_event_patterns():
    result = KeywordFilter().analyze("Central bank intervenes and unexpectedly unveils a bond buyback", news_mode=True)

    assert "intervenes" in result.keywords
    assert "unexpected" in result.keywords
    assert "bond buyback" in result.keywords
    assert KeywordFilter().pre_filter("Policy change shocks markets", news_mode=True)


def test_filter_rejects_non_string_text():
    with pytest.raises(TypeError):
        KeywordFilter().pre_filter(None)
