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

GEOPOLITICAL_TERMS = (
    "war", "iran", "bomb", "bombing", "attack", "attacks", "missile", "missiles",
    "strike", "strikes", "military", "navy", "conflict", "escalation",
)

GEOPOLITICAL_GUIDANCE = """Use a balanced cross-asset causal analysis. For this geopolitical story, assess both:
- safe-haven demand, which can support gold;
- oil or crude-price upside, which can raise inflation expectations and keep yields or the USD higher, pressuring gold;
- the resulting DXY, Treasury-yield/real-yield, liquidity, and risk-on/risk-off effects.

Do not assume that geopolitical escalation is automatically bullish for gold. Separate the immediate safe-haven reaction from the sustained XAUUSD direction, identify missing live inputs (oil, DXY, yields, and price action), and lower confidence or use NEUTRAL/HOLD when the headline does not establish which force dominates. Do not invent current market prices or movements. Explain the likely effect on oil prices and DXY, including whether those channels support or pressure gold.

Put the reasoning in exactly this compact labeled format so it can be shown directly to the trader:
OIL: likely direction and mechanism, or "unknown from headline"
DXY: likely direction and mechanism, or "unknown from headline"
GOLD: safe-haven effect versus oil/USD/yield offset
SUMMARY: immediate XAUUSD bias, sustained bias, key missing confirmation, and why the action is justified."""

PROMPT_TEMPLATE = """Analyze this news story's likely impact on Gold (XAUUSD) and US Dollar (DXY) sentiment.

Headline: {headline}
Article preview: {content}
Source: {source}
Keywords: {keywords}
Assets mentioned: {asset_mentions}

{geopolitical_guidance}

Respond strictly in this JSON format:
{{
  \"asset\": \"XAUUSD\",
  \"sentiment\": \"BULLISH/BEARISH/NEUTRAL\",
  \"impact_score\": 8,
  \"action\": \"LOOK FOR BUYS/SELLS/HOLD\",
  \"reasoning\": \"Clear explanation\"
}}"""

RESPONSE_FORMAT_RULES = """
Return one complete JSON object only. Do not wrap it in Markdown fences and do not add commentary.
Every field must be a scalar: asset, sentiment, action, and reasoning are strings; impact_score is
an integer from 1 to 10; confidence is a number from 0 to 1. Do not return nested objects or arrays.
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "asset": {"type": "string"},
        "sentiment": {"type": "string", "enum": ["BULLISH", "BEARISH", "NEUTRAL"]},
        "impact_score": {"type": "integer", "minimum": 1, "maximum": 10},
        "action": {"type": "string"},
        "reasoning": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["asset", "sentiment", "impact_score", "action", "reasoning"],
    "additionalProperties": False,
}

CALENDAR_PROMPT_TEMPLATE = """Analyze this economic calendar release for Gold (XAUUSD) and the US Dollar (DXY).

Event: {headline}
Currency: {currency}
Impact: {impact_level}
Release time: {release_time}
Previous: {previous}
Forecast: {forecast}
Actual: {actual}

This is a calendar-only analysis. Do not invent live market data or pretend to know the market's current position.
When Actual is not released, compare forecast with previous and provide a cautious preparation action. Give event-specific scenarios for actual above forecast, near forecast, and below forecast. Explain why each scenario could affect USD, yields, and gold, and tell the trader to wait for the actual release and price confirmation.
When Actual is released, compare actual with forecast and previous, identify which scenario occurred, and provide a cautious post-release action. If the numbers do not establish a clean edge, use WAIT/HOLD.
Do not use labels such as "Claude bias" in the reasoning. Write trader-facing sections with these exact labels: ACTION, WHY, ABOVE FORECAST, NEAR FORECAST, BELOW FORECAST, CONFIRMATION.
Respond strictly in this JSON format:
{{
    "asset": "XAUUSD",
    "sentiment": "BULLISH/BEARISH/NEUTRAL",
    "impact_score": 9,
    "action": "PREPARE FOR GOLD BUYS/SELLS or WAIT FOR ACTUAL",
    "reasoning": "ACTION: ...\\nWHY: ...\\nABOVE FORECAST: ...\\nNEAR FORECAST: ...\\nBELOW FORECAST: ...\\nCONFIRMATION: ...",
    "confidence": 0.78
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
        prompt = self._prompt_for(event)
        for attempt in range(1, self.max_retries + 1):
            try:
                self._metrics["requests"] += 1
                response = self._client.messages.create(
                    model=self.model,
                    max_tokens=1000,
                    temperature=0,
                    output_config={"format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}},
                    messages=[{"role": "user", "content": prompt + RESPONSE_FORMAT_RULES}],
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

    @staticmethod
    def _prompt_for(event: NewsEvent) -> str:
        if event.calendar is None:
            return PROMPT_TEMPLATE.format(
                headline=event.headline,
                content=event.content or "unavailable",
                source=event.source,
                keywords=", ".join(event.keywords) or "none",
                asset_mentions=", ".join(event.asset_mentions) or "none",
                geopolitical_guidance=(
                    GEOPOLITICAL_GUIDANCE
                    if ClaudeAnalyzer._is_geopolitical_event(event)
                    else "Assess only the direct event-specific effects. Do not infer unrelated oil or geopolitical effects."
                ),
            )
        calendar = event.calendar
        return CALENDAR_PROMPT_TEMPLATE.format(
            headline=event.headline,
            currency=calendar.currency,
            impact_level=calendar.impact_level.upper(),
            release_time=event.timestamp.isoformat(),
            previous=calendar.previous or "unavailable",
            forecast=calendar.forecast or "unavailable",
            actual=calendar.actual or "not released",
        )

    @staticmethod
    def _is_geopolitical_event(event: NewsEvent) -> bool:
        text = f"{event.headline} {event.content or ''}".casefold()
        return any(re.search(rf"\b{re.escape(term)}\b", text) for term in GEOPOLITICAL_TERMS)

    def cost_metrics(self) -> CostMetrics:
        return CostMetrics(**self._metrics)

    def _fallback(self, event: NewsEvent, reason: str) -> AnalysisResult:
        return AnalysisResult(
            event_id=event.id,
            asset="XAUUSD",
            sentiment="NEUTRAL",
            impact_score=1,
            action="HOLD",
            reasoning=f"[CLAUDE_FAILURE] {reason}",
            confidence=0.0,
            analyzed_at=datetime.now(timezone.utc),
        )

    @staticmethod
    def _parse_response(response: Any) -> dict[str, Any]:
        content = response.content[0].text if getattr(response, "content", None) else ""
        if not isinstance(content, str):
            raise ValueError(f"Claude response text must be a string, got {type(content).__name__}")
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE)
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            start = content.find("{")
            if start < 0:
                raise
            payload, _ = json.JSONDecoder().raw_decode(content[start:])
        if not isinstance(payload, dict):
            raise ValueError(
                f"Claude response JSON must be an object, got {type(payload).__name__}"
            )
        required = {"asset", "sentiment", "impact_score", "action", "reasoning"}
        if not required.issubset(payload):
            raise ValueError("Claude response is missing required fields")
        for field in ("asset", "sentiment", "action", "reasoning"):
            if not isinstance(payload[field], str):
                raise ValueError(
                    f"Claude response field {field!r} must be a string, "
                    f"got {type(payload[field]).__name__}"
                )
        if isinstance(payload["impact_score"], bool) or not isinstance(
            payload["impact_score"], (int, float)
        ):
            raise ValueError("Claude response field 'impact_score' must be numeric")
        if "confidence" in payload and (
            isinstance(payload["confidence"], bool)
            or not isinstance(payload["confidence"], (int, float))
        ):
            raise ValueError("Claude response field 'confidence' must be numeric")
        return {key: payload[key] for key in required} | (
            {"confidence": payload["confidence"]} if "confidence" in payload else {}
        )

    def _record_usage(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        self._metrics["input_tokens"] += input_tokens
        self._metrics["output_tokens"] += output_tokens
        self._metrics["estimated_cost_usd"] += (input_tokens * 3 + output_tokens * 15) / 1_000_000
