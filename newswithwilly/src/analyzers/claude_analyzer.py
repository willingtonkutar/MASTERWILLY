"""Claude-powered structured analysis for normalized news events."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from models import AnalysisResult, NewsEvent

logger = logging.getLogger(__name__)

PROMPT_TEMPLATE = """Analyze this headline's direct impact on Gold (XAUUSD) and US Dollar (DXY) sentiment:

Headline: {headline}
Source: {source}
Keywords: {keywords}

Respond strictly in this JSON format:
{{
  \"asset\": \"XAUUSD\",
  \"sentiment\": \"BULLISH/BEARISH/NEUTRAL\",
  \"impact_score\": 8,
  \"action\": \"LOOK FOR BUYS/SELLS/HOLD\",
  \"reasoning\": \"Clear explanation\"
}}"""


@dataclass(frozen=True)
class CostMetrics:
    requests: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float


class ClaudeAnalyzer:
    """Call Claude with bounded retries and validate every response."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str | None = None,
        timeout_seconds: float | None = None,
        max_retries: int = 3,
        backoff_seconds: float = 1.0,
        client: Any | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self.model = model or os.getenv("CLAUDE_MODEL", "claude-3-5-sonnet-20241022")
        self.timeout_seconds = timeout_seconds or float(os.getenv("CLAUDE_TIMEOUT_SECONDS", "30"))
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self._client = client
        self._metrics = {"requests": 0, "input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0}
        if self._client is None and self.api_key:
            try:
                from anthropic import Anthropic
                self._client = Anthropic(api_key=self.api_key, timeout=self.timeout_seconds)
            except ImportError as exc:
                raise RuntimeError("Install the anthropic package to use ClaudeAnalyzer") from exc

    def analyze_event(self, event: NewsEvent) -> AnalysisResult:
        """Analyze an event or return a neutral, valid fallback on failure."""
        if self._client is None:
            logger.warning("Claude API key/client is not configured; using neutral fallback")
            return self._fallback(event, "Claude client is not configured")
        prompt = PROMPT_TEMPLATE.format(headline=event.headline, source=event.source, keywords=", ".join(event.keywords))
        for attempt in range(1, self.max_retries + 1):
            try:
                self._metrics["requests"] += 1
                response = self._client.messages.create(
                    model=self.model,
                    max_tokens=500,
                    temperature=0,
                    messages=[{"role": "user", "content": prompt}],
                )
                self._record_usage(response)
                payload = self._parse_response(response)
                payload.setdefault("confidence", float(payload["impact_score"]) / 10.0)
                return AnalysisResult(event_id=event.id, analyzed_at=datetime.now(timezone.utc), **payload)
            except Exception as exc:
                logger.warning("Claude analysis failed (%d/%d): %s", attempt, self.max_retries, exc)
                if attempt < self.max_retries:
                    time.sleep(self.backoff_seconds * (2 ** (attempt - 1)))
        return self._fallback(event, "Claude analysis failed after retries")

    def cost_metrics(self) -> CostMetrics:
        return CostMetrics(**self._metrics)

    def _fallback(self, event: NewsEvent, reason: str) -> AnalysisResult:
        return AnalysisResult(event_id=event.id, asset="XAUUSD", sentiment="NEUTRAL", impact_score=1, action="HOLD", reasoning=reason, confidence=0.0, analyzed_at=datetime.now(timezone.utc))

    @staticmethod
    def _parse_response(response: Any) -> dict[str, Any]:
        content = response.content[0].text if getattr(response, "content", None) else ""
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE)
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            start = content.find("{")
            if start < 0:
                raise
            payload, _ = json.JSONDecoder().raw_decode(content[start:])
        required = {"asset", "sentiment", "impact_score", "action", "reasoning"}
        if not required.issubset(payload):
            raise ValueError("Claude response is missing required fields")
        return {key: payload[key] for key in required} | ({"confidence": payload["confidence"]} if "confidence" in payload else {})

    def _record_usage(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        self._metrics["input_tokens"] += input_tokens
        self._metrics["output_tokens"] += output_tokens
        self._metrics["estimated_cost_usd"] += (input_tokens * 3 + output_tokens * 15) / 1_000_000
