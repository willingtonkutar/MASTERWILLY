from datetime import datetime, timezone

import pytest

from scrapers.twitter_monitor import TwitterMonitor


class FakeQueue:
    def __init__(self):
        self.items = []

    def put(self, event, impact_score):
        self.items.append((event, impact_score))


class FakeTweet:
    id = 123
    author_id = 42
    text = "Gold rises as CPI surprises markets"
    created_at = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
    author_username = "Reuters"


def test_monitor_filters_tweets_deduplicates_and_enqueues():
    event_queue = FakeQueue()
    monitor = TwitterMonitor(handles=["Reuters"], client=object(), event_queue=event_queue)

    assert monitor.on_tweet(FakeTweet())
    assert not monitor.on_tweet(FakeTweet())
    assert len(event_queue.items) == 1
    event, score = event_queue.items[0]
    assert event.source == "twitter"
    assert event.url.endswith("/123")
    assert "cpi" in event.keywords
    assert score >= 1


def test_monitor_ignores_unmatched_tweets():
    class UnmatchedTweet(FakeTweet):
        id = 124
        text = "A quiet day in the garden"

    event_queue = FakeQueue()
    monitor = TwitterMonitor(handles=["Reuters"], client=object(), event_queue=event_queue)

    assert not monitor.on_tweet(UnmatchedTweet())
    assert not event_queue.items


def test_monitor_uses_api_client_for_account_resolution_when_stream_client_has_no_get_user():
    class FakeStreamingClient:
        def __init__(self):
            self.rules = []

        def get_rules(self):
            return type("Response", (), {"data": []})()

        def add_rules(self, rules):
            self.rules.extend(rules)

        def delete_rules(self, rule_ids):
            self.deleted = list(rule_ids)

        def filter(self, **kwargs):
            return None

    class FakeApiClient:
        def get_user(self, username):
            return type("Response", (), {"data": type("User", (), {"id": "42"})()})()

    monitor = TwitterMonitor(handles=["Reuters"], bearer_token="abc", client=FakeStreamingClient(), event_queue=FakeQueue())
    monitor._api_client = FakeApiClient()

    monitor._configure_rules()

    assert monitor._user_ids["reuters"] == "42"
    assert len(monitor._client.rules) == 1


def test_monitor_degrades_without_crashing_when_twitter_api_rejects_user_lookup():
    class FakeStreamingClient:
        def get_rules(self):
            return type("Response", (), {"data": []})()

        def delete_rules(self, rule_ids):
            return None

        def add_rules(self, rules):
            raise AssertionError("add_rules should not be called when lookup fails")

    class FakeApiClient:
        def get_user(self, username):
            raise RuntimeError("402 Payment Required")

    monitor = TwitterMonitor(handles=["Reuters"], bearer_token="abc", client=FakeStreamingClient(), event_queue=FakeQueue())
    monitor._api_client = FakeApiClient()

    with pytest.raises(ValueError, match="No tracked Twitter accounts could be resolved"):
        monitor._configure_rules()

    assert not monitor._user_ids
    assert not monitor._usernames


def test_monitor_uses_required_default_accounts_and_tiers():
    monitor = TwitterMonitor(client=object())

    assert monitor.handles == ("reuters", "bloomberg", "cnbc", "federalreserve", "ecb", "realdonaldtrump")
    assert monitor.match_text("War escalates near border").tier == 1
    assert monitor.match_text("Officials discuss meeting plans").tier == 2
    assert monitor.match_text("Gold and bullion demand rise").tier == 3


def test_monitor_requires_two_tier_two_terms_and_rejects_partial_words():
    monitor = TwitterMonitor(client=object())

    assert monitor.match_text("Officials discuss the situation") is None
    assert monitor.match_text("A golden opportunity for investors") is None


def test_monitor_rejects_untracked_author_and_truncates_headline():
    class UntrackedTweet(FakeTweet):
        author_username = "randomaccount"
        text = "war " * 100

    event_queue = FakeQueue()
    monitor = TwitterMonitor(event_queue=event_queue, client=object())

    assert not monitor.on_tweet(UntrackedTweet())
    assert not event_queue.items


def test_monitor_helpers_use_v2_client():
    class Client:
        def get_me(self):
            return type("Response", (), {"data": object()})()

        def get_user(self, username):
            return type("Response", (), {"data": type("User", (), {"id": "42"})()})()

        def get_users_tweets(self, user_id, **kwargs):
            return type("Response", (), {"data": [1, 2, 3]})()

    monitor = TwitterMonitor(handles=["Reuters"], client=Client())

    assert monitor.test_connection()
    assert monitor.get_recent_tweets("Reuters", count=2) == [1, 2]
    with pytest.raises(ValueError):
        monitor.get_recent_tweets("Reuters", count=0)
