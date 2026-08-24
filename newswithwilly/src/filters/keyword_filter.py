"""Cheap keyword and asset detection before external analysis calls."""

from __future__ import annotations

import re
from dataclasses import dataclass


DEFAULT_KEYWORDS = ("iran", "war", "tariff", "bomb", "cpi", "fed", "rates", "dxy", "gold")
NEWS_KEYWORDS = (
    "buys bonds", "bond buyback", "treasury purchase", "intervenes", "intervention",
    "currency intervention", "announces", "unveils", "reveals", "shocks", "surprises",
    "unexpected", "policy change", "central bank", "geopolitical", "market intervention",
)
NEWS_KEYWORD_VARIATIONS = {
    "announces": ("announces", "announce", "announced", "announcing"),
    "intervenes": ("intervenes", "intervene", "intervened", "intervening"),
    "unexpected": ("unexpected", "unexpectedly"),
    "shocks": ("shocks", "shock", "shocked", "shocking"),
    "surprises": ("surprises", "surprise", "surprised", "surprising"),
}
KEYWORD_VARIATIONS = {
    "cpi": ("consumer price index", "inflation"),
    "fed": ("federal reserve", "fomc"),
    "rates": ("rate", "interest rate", "interest rates"),
    "dxy": ("dollar", "us dollar", "usd"),
    "gold": ("xauusd", "xau/usd"),
}
NEGATIONS = ("no", "not", "unlikely")


@dataclass(frozen=True)
class FilterMatch:
    """Details from one cheap text-filter pass."""

    keywords: list[str]
    asset_mentions: list[str]
    confidence: float
    negated_keywords: list[str]


class KeywordFilter:
    """Perform case-insensitive, word-boundary keyword matching."""

    def __init__(self, keywords: list[str] | tuple[str, ...] | None = None) -> None:
        configured = keywords or DEFAULT_KEYWORDS
        self.keywords = tuple(dict.fromkeys(keyword.lower().strip() for keyword in configured if keyword.strip()))
        self._patterns = {
            keyword: re.compile(rf"\b{re.escape(keyword)}\b", re.IGNORECASE)
            for keyword in self.keywords
        }

    def pre_filter(self, headline: str, *, news_mode: bool = False) -> bool:
        """Return whether text contains a whitelisted term, including variations."""
        return bool(self.extract_keywords(headline, news_mode=news_mode))

    def extract_keywords(self, text: str, *, news_mode: bool = False) -> list[str]:
        """Return canonical whitelist terms found in text without substring matches."""
        return self.analyze(text, news_mode=news_mode).keywords

    def check_asset_mentions(self, text: str) -> list[str]:
        """Return normalized assets mentioned directly or through common aliases."""
        lowered = text.lower()
        assets: list[str] = []
        if self._matches(lowered, ("gold", "xauusd", "xau/usd")):
            assets.append("XAUUSD")
        if self._matches(lowered, ("dxy", "dollar", "us dollar", "usd")):
            assets.append("DXY")
        return assets

    def confidence_score(self, text: str) -> float:
        """Return a bounded confidence score for the matched terms."""
        return self.analyze(text).confidence

    def analyze(self, text: str, *, news_mode: bool = False) -> FilterMatch:
        """Return matches, asset aliases, negation details, and confidence."""
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        lowered = text.lower()
        matched = [keyword for keyword, pattern in self._patterns.items() if pattern.search(text)]
        for canonical, variations in KEYWORD_VARIATIONS.items():
            if canonical in self.keywords and self._matches(lowered, variations):
                if canonical not in matched:
                    matched.append(canonical)
        if news_mode:
            matched.extend(
                keyword
                for keyword in NEWS_KEYWORDS
                if keyword not in matched
                and self._matches(lowered, NEWS_KEYWORD_VARIATIONS.get(keyword, (keyword,)))
            )
        negated = [keyword for keyword in matched if self._is_negated(lowered, keyword)]
        positive_count = len(matched) - len(negated)
        confidence = min(1.0, 0.35 + positive_count * 0.15 + len(self.check_asset_mentions(text)) * 0.1)
        if matched and positive_count == 0:
            confidence = 0.2
        return FilterMatch(matched, self.check_asset_mentions(text), round(confidence, 3), negated)

    @staticmethod
    def _matches(text: str, terms: tuple[str, ...]) -> bool:
        return any(re.search(rf"\b{re.escape(term)}\b", text, re.IGNORECASE) for term in terms)

    @staticmethod
    def _is_negated(text: str, keyword: str) -> bool:
        match = re.search(rf"\b{re.escape(keyword)}\b", text, re.IGNORECASE)
        if match is None:
            return False
        prefix = text[max(0, match.start() - 30):match.start()]
        return bool(re.search(r"\b(?:no|not|unlikely)\b(?:\s+\w+){0,3}\s*$", prefix, re.IGNORECASE))
