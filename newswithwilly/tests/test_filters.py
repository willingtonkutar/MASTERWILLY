from filters.keyword_filter import KeywordFilter


def test_filter_fixture_style_cases():
    keyword_filter = KeywordFilter()
    cases = {
        "Fed holds rates": True,
        "Gold reaches new high": True,
        "ordinary market commentary": False,
        "golden opportunity": False,
    }
    for text, expected in cases.items():
        assert keyword_filter.pre_filter(text) is expected
