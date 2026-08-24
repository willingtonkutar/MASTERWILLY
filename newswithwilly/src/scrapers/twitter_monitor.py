"""Real-time X/Twitter monitoring with account and impact-tier filtering."""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable
from uuid import UUID

from filters.keyword_filter import KeywordFilter
from models import NewsEvent

try:
    import tweepy
except ImportError:  # pragma: no cover
    tweepy = None

logger = logging.getLogger(__name__)


if tweepy is not None:
    class _StreamingClient(tweepy.StreamingClient):
        def __init__(self, owner: "TwitterMonitor", bearer_token: str) -> None:
            super().__init__(bearer_token)
            self.owner = owner

        def on_tweet(self, tweet: Any) -> bool:
            return self.owner.on_tweet(tweet)

        def on_response(self, response: Any) -> None:
            self.owner.on_response(response)

DEFAULT_HANDLES = ("Reuters", "Bloomberg", "CNBC", "federalreserve", "ecb", "realDonaldTrump")
DEFAULT_TIER1 = (
    "war", "attack", "strike", "bomb", "missile", "shoot", "invasion", "conflict", "tension",
    "escalate", "nuclear", "uranium", "enrichment", "tariff", "sanction", "embargo", "cpi",
    "inflation", "ppi", "fomc", "rate hike", "rate cut", "pivot", "gdp", "recession", "growth",
    "nfp", "unemployment", "jobs",
)
DEFAULT_TIER2 = (
    "talks", "meeting", "summit", "discuss", "warn", "threat", "caution", "announce", "declare",
    "manufacturing", "pmi", "services", "retail sales", "consumer spending", "housing", "construction",
)
DEFAULT_TIER3 = (
    "gold", "xau", "bullion", "reserve", "central bank buying", "gold demand", "gold price", "mining",
    "output", "production",
)
ALLOWED_HANDLES = frozenset(handle.lower() for handle in DEFAULT_HANDLES)


@dataclass(frozen=True)
class TweetMatch:
    keywords: list[str]
    tier: int
    asset_mentions: list[str]


class TwitterMonitor:
    """Stream only configured accounts and economically relevant tweets."""

    def __init__(
        self,
        handles: Iterable[str] | None = None,
        *,
        bearer_token: str | None = None,
        event_queue: Any | None = None,
        client: Any | None = None,
        max_reconnects: int = 5,
        reconnect_backoff_seconds: float = 2.0,
    ) -> None:
        configured_handles = self._csv(handles, "TWITTER_HANDLES", DEFAULT_HANDLES)
        self.handles = tuple(handle for handle in configured_handles if handle in ALLOWED_HANDLES)
        if not self.handles:
            raise ValueError("TWITTER_HANDLES must contain at least one supported account")
        self.bearer_token = os.getenv("TWITTER_BEARER_TOKEN") if bearer_token is None else bearer_token
        self.tier1 = self._csv(None, "TWITTER_KEYWORDS_TIER1", DEFAULT_TIER1)
        self.tier2 = self._csv(None, "TWITTER_KEYWORDS_TIER2", DEFAULT_TIER2)
        self.tier3 = self._csv(None, "TWITTER_KEYWORDS_TIER3", DEFAULT_TIER3)
        self.event_queue = event_queue
        self.max_reconnects = max(0, max_reconnects)
        self.reconnect_backoff_seconds = max(0.0, reconnect_backoff_seconds)
        self.last_tweet_by_user: dict[str, str] = {}
        self._client = client if client is not None else self._create_client()
        self._api_client = self._create_api_client() if self.bearer_token else None
        self._stop_event = threading.Event()
        self._stream_thread: threading.Thread | None = None
        self._user_ids: dict[str, str] = {}
        self._usernames: dict[str, str] = {}

    @staticmethod
    def _csv(values: Iterable[str] | None, env_name: str, default: Iterable[str]) -> tuple[str, ...]:
        raw = values if values is not None else os.getenv(env_name)
        if isinstance(raw, str):
            raw = raw.split(",")
        return tuple(dict.fromkeys(str(item).strip().lstrip("@").lower() for item in (raw or default) if str(item).strip()))

    def start_stream(self, *, background: bool = True) -> None:
        if self._stream_thread and self._stream_thread.is_alive():
            raise RuntimeError("Twitter stream is already running")
        self._stop_event.clear()
        if background:
            self._stream_thread = threading.Thread(target=self._run_stream, name="twitter-monitor", daemon=True)
            self._stream_thread.start()
        else:
            self._run_stream()

    def stop_stream(self, timeout: float | None = None) -> None:
        self._stop_event.set()
        if self._client is not None:
            try:
                self._client.disconnect()
            except (AttributeError, RuntimeError):
                logger.debug("Twitter client did not support disconnect", exc_info=True)
        if self._stream_thread:
            self._stream_thread.join(timeout)

    start = start_stream
    stop = stop_stream

    def test_connection(self) -> bool:
        """Verify bearer authentication with a lightweight API request."""
        if self._client is None:
            return False
        try:
            response = self._client.get_me()
            return getattr(response, "data", None) is not None
        except Exception:
            logger.exception("Twitter connection test failed")
            return False

    def get_recent_tweets(self, handle: str, count: int = 5) -> list[Any]:
        if count < 1:
            raise ValueError("count must be at least 1")
        if self._client is None:
            raise RuntimeError("TWITTER_BEARER_TOKEN is required")
        username = handle.strip().lstrip("@").lower()
        user = self._client.get_user(username=username)
        user_id = getattr(getattr(user, "data", None), "id", None)
        if user_id is None:
            raise ValueError(f"Twitter user not found: @{handle}")
        response = self._client.get_users_tweets(user_id, max_results=max(5, min(100, count)), tweet_fields=["created_at", "author_id"])
        return list(getattr(response, "data", None) or [])[:count]

    def on_tweet(self, tweet: Any) -> bool:
        tweet_id = str(getattr(tweet, "id", ""))
        author_id = str(getattr(tweet, "author_id", "unknown"))
        username = str(getattr(tweet, "author_username", "") or getattr(tweet, "username", "") or self._usernames.get(author_id, "")).lstrip("@").lower()
        if username and username not in self.handles:
            return False
        if not username and author_id not in self._usernames.values() and author_id not in self._user_ids.values():
            return False
        if not tweet_id or self.last_tweet_by_user.get(author_id) == tweet_id:
            return False
        text = str(getattr(tweet, "text", "") or "")
        match = self.match_text(text)
        if match is None:
            return False
        timestamp = getattr(tweet, "created_at", None) or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        event = NewsEvent(
            source="twitter",
            headline=text[:200],
            content=text,
            url=f"https://x.com/{username or 'i'}/status/{tweet_id}",
            timestamp=timestamp,
            keywords=match.keywords,
            asset_mentions=match.asset_mentions,
        )
        self.last_tweet_by_user[author_id] = tweet_id
        logger.info("Matched tweet @%s tier=%d keywords=%s", username or author_id, match.tier, match.keywords)
        if self.event_queue is not None:
            self.event_queue.put(event, impact_score=9 if match.tier == 1 else 5)
        return True

    def match_text(self, text: str) -> TweetMatch | None:
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        filter_engine = KeywordFilter(self.tier1 + self.tier2 + self.tier3)
        found = filter_engine.analyze(text).keywords
        tier1 = [term for term in found if term in self.tier1]
        tier2 = [term for term in found if term in self.tier2]
        tier3 = [term for term in found if term in self.tier3]
        if tier1:
            tier = 1
        elif len(tier2) >= 2:
            tier = 2
        elif tier3 and any(term in text.lower() for term in ("gold", "xau", "bullion")):
            tier = 3
        else:
            return None
        return TweetMatch(tier1 + tier2 + tier3, tier, self._assets(text))

    def _create_client(self) -> Any | None:
        if tweepy is None:
            if self.bearer_token:
                raise RuntimeError("Install tweepy to use TwitterMonitor")
            return None
        return _StreamingClient(self, self.bearer_token) if self.bearer_token else None

    def _create_api_client(self) -> Any | None:
        if tweepy is None:
            if self.bearer_token:
                raise RuntimeError("Install tweepy to use TwitterMonitor")
            return None
        if not self.bearer_token:
            return None
        return tweepy.Client(self.bearer_token)

    def on_response(self, response: Any) -> None:
        """Resolve expanded author usernames before processing a stream tweet."""
        includes = getattr(response, "includes", None) or {}
        for user in includes.get("users", []) if isinstance(includes, dict) else []:
            user_id = str(getattr(user, "id", ""))
            username = getattr(user, "username", None)
            if user_id and username:
                self._usernames[user_id] = str(username).lower()
        tweet = getattr(response, "data", None)
        if tweet is not None:
            self.on_tweet(tweet)

    def _run_stream(self) -> None:
        if self._client is None:
            raise RuntimeError("TWITTER_BEARER_TOKEN is required")
        try:
            self._configure_rules()
        except Exception as exc:
            logger.warning("Twitter stream unavailable; continuing without live monitoring: %s", exc)
            return
        reconnects = 0
        while not self._stop_event.is_set():
            try:
                self._client.filter(tweet_fields=["created_at", "author_id"], expansions=["author_id"], user_fields=["username"])
                reconnects = 0
            except Exception as exc:
                reconnects += 1
                logger.warning("Twitter stream failure %d/%d: %s", reconnects, self.max_reconnects, exc)
                if reconnects > self.max_reconnects:
                    logger.error("Twitter monitor stopped after retry limit")
                    return
                self._stop_event.wait(self.reconnect_backoff_seconds * (2 ** (reconnects - 1)))

    def _configure_rules(self) -> None:
        if not self.handles:
            raise ValueError("TWITTER_HANDLES must contain at least one account")
        if self._api_client is None:
            raise RuntimeError("TWITTER_BEARER_TOKEN is required")
        try:
            existing = self._client.get_rules().data or []
        except Exception as exc:
            raise RuntimeError(f"Unable to read Twitter rules: {exc}") from exc
        if existing:
            try:
                self._client.delete_rules([rule.id for rule in existing])
            except Exception as exc:
                logger.warning("Could not clear existing Twitter rules: %s", exc)
        rules = []
        for handle in self.handles:
            try:
                user = self._api_client.get_user(username=handle)
            except Exception as exc:
                logger.warning("Could not resolve Twitter handle %s: %s", handle, exc)
                continue
            data = getattr(user, "data", None)
            if data is not None:
                self._user_ids[handle] = str(data.id)
                self._usernames[str(data.id)] = handle
                rules.append(tweepy.StreamRule(f"from:{data.id}"))
        if not rules:
            raise ValueError("No tracked Twitter accounts could be resolved")
        try:
            self._client.add_rules(rules)
        except Exception as exc:
            raise RuntimeError(f"Unable to add Twitter stream rules: {exc}") from exc

    @staticmethod
    def _assets(text: str) -> list[str]:
        lowered = text.lower()
        assets = []
        if any(term in lowered.split() for term in ("xauusd", "xau")) or "gold" in lowered or "bullion" in lowered:
            assets.extend(["XAUUSD", "Gold"])
        if "dxy" in lowered or "dollar" in lowered:
            assets.extend(["DXY", "Dollar"])
        return assets
