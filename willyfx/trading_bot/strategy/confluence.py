"""SMC confluence engine.

Combines higher-timeframe trend, H1/M15 order block + FVG overlap,
liquidity sweeps, lower-timeframe confirmation, and volume into a scored
BUY/SELL/NO_TRADE decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import pandas as pd

from strategy.fair_value_gaps import FairValueGap, latest_unmitigated_fvgs
from strategy.liquidity import LiquidityZone, detect_liquidity_zones
from strategy.order_blocks import OrderBlock, latest_active_order_blocks


Direction = Literal["bullish", "bearish", "neutral"]
TradeAction = Literal["BUY", "SELL", "NO_TRADE"]


@dataclass(frozen=True)
class ConfluenceConfig:
    minimum_score: int = 8
    trend_score: int = 2
    order_block_score: int = 2
    fvg_overlap_score: int = 2
    liquidity_sweep_score: int = 3
    ltf_confirmation_score: int = 3
    volume_spike_score: int = 1
    order_block_range: int = 25
    liquidity_length: int = 14
    fvg_threshold_percent: float = 0
    volume_lookback: int = 20
    volume_multiplier: float = 1.5


@dataclass(frozen=True)
class ConfluenceDecision:
    action: TradeAction
    direction: Direction
    score: int
    reasons: list[str]
    order_block: OrderBlock | None = None
    fair_value_gap: FairValueGap | None = None
    liquidity_zone: LiquidityZone | None = None
    details: dict[str, object] = field(default_factory=dict)


def analyze_confluence(
    higher_timeframes: dict[str, pd.DataFrame],
    zone_timeframes: dict[str, pd.DataFrame],
    entry_timeframes: dict[str, pd.DataFrame] | None = None,
    config: ConfluenceConfig | None = None,
) -> ConfluenceDecision:
    config = config or ConfluenceConfig()
    entry_timeframes = entry_timeframes or {}

    bias, trend_reasons = _higher_timeframe_bias(higher_timeframes)
    buy_decision = _score_direction("bullish", bias, trend_reasons, zone_timeframes, entry_timeframes, config)
    sell_decision = _score_direction("bearish", bias, trend_reasons, zone_timeframes, entry_timeframes, config)
    best = buy_decision if buy_decision.score >= sell_decision.score else sell_decision

    if bias == "neutral" or best.score < config.minimum_score:
        return ConfluenceDecision(
            action="NO_TRADE",
            direction=best.direction if bias != "neutral" else "neutral",
            score=best.score if bias != "neutral" else 0,
            reasons=best.reasons + [f"Score below minimum {config.minimum_score}"],
            order_block=best.order_block,
            fair_value_gap=best.fair_value_gap,
            liquidity_zone=best.liquidity_zone,
            details=best.details | {"trend_bias": bias},
        )

    return ConfluenceDecision(
        action="BUY" if best.direction == "bullish" else "SELL",
        direction=best.direction,
        score=best.score,
        reasons=best.reasons,
        order_block=best.order_block,
        fair_value_gap=best.fair_value_gap,
        liquidity_zone=best.liquidity_zone,
        details=best.details | {"trend_bias": bias},
    )


def find_current_poi(
    zone_timeframes: dict[str, pd.DataFrame],
    direction: Direction | None = None,
    config: ConfluenceConfig | None = None,
) -> dict[str, object]:
    """Find the most relevant point of interest the bot is watching."""

    config = config or ConfluenceConfig()
    candidates: list[dict[str, object]] = []

    for timeframe, data in zone_timeframes.items():
        blocks = latest_active_order_blocks(data, candle_range=config.order_block_range)
        gaps = latest_unmitigated_fvgs(data, threshold_percent=config.fvg_threshold_percent)
        last_close = float(data.reset_index(drop=True).iloc[-1]["close"])

        for block in blocks:
            if direction and block.side != direction:
                continue
            overlaps = [
                gap for gap in gaps
                if gap.side == block.side and _zones_overlap(block.top, block.bottom, gap.top, gap.bottom)
            ]
            distance = min(abs(last_close - block.top), abs(last_close - block.bottom))
            candidates.append({
                "timeframe": timeframe,
                "side": block.side,
                "type": "OB + FVG overlap" if overlaps else "Order block",
                "top": block.top,
                "bottom": block.bottom,
                "distance": distance,
                "has_fvg_overlap": bool(overlaps),
                "order_block": block,
                "fair_value_gap": overlaps[-1] if overlaps else None,
            })

    if not candidates:
        return {"type": "None", "message": "No active point of interest found"}

    candidates.sort(key=lambda item: (not item["has_fvg_overlap"], item["distance"]))
    return candidates[0]


def _score_direction(
    direction: Direction,
    bias: Direction,
    trend_reasons: list[str],
    zone_timeframes: dict[str, pd.DataFrame],
    entry_timeframes: dict[str, pd.DataFrame],
    config: ConfluenceConfig,
) -> ConfluenceDecision:
    score = 0
    reasons: list[str] = []
    selected_ob = None
    selected_fvg = None
    selected_liquidity = None
    details: dict[str, object] = {"trend_bias": bias}

    if bias == direction:
        score += config.trend_score
        reasons.append(f"Higher-timeframe bias is {direction}")
        reasons.extend(trend_reasons)

    zone_match = _find_zone_match(direction, zone_timeframes, config)
    if zone_match:
        timeframe, selected_ob, selected_fvg = zone_match
        score += config.order_block_score + config.fvg_overlap_score
        reasons.append(f"{timeframe} order block overlaps with an FVG")
        details["zone_timeframe"] = timeframe

    liquidity_match = _find_recent_liquidity_sweep(direction, zone_timeframes, config)
    if liquidity_match:
        timeframe, selected_liquidity = liquidity_match
        score += config.liquidity_sweep_score
        reasons.append(f"{timeframe} liquidity sweep detected")
        details["liquidity_timeframe"] = timeframe

    ltf_reason = _find_ltf_confirmation(direction, entry_timeframes)
    if ltf_reason:
        score += config.ltf_confirmation_score
        reasons.append(ltf_reason)

    volume_reason = _find_volume_spike(entry_timeframes or zone_timeframes, config)
    if volume_reason:
        score += config.volume_spike_score
        reasons.append(volume_reason)

    return ConfluenceDecision("NO_TRADE", direction, score, reasons, selected_ob, selected_fvg, selected_liquidity, details)


def _higher_timeframe_bias(timeframes: dict[str, pd.DataFrame]) -> tuple[Direction, list[str]]:
    trends: dict[str, Direction] = {}
    reasons: list[str] = []
    for name, data in timeframes.items():
        if data is None or data.empty:
            continue
        trend = _infer_trend(data)
        trends[name] = trend
        reasons.append(f"{name} trend is {trend}")

    bullish = sum(trend == "bullish" for trend in trends.values())
    bearish = sum(trend == "bearish" for trend in trends.values())
    if bullish > bearish:
        return "bullish", reasons
    if bearish > bullish:
        return "bearish", reasons
    return "neutral", reasons


def _infer_trend(data: pd.DataFrame, swing_length: int = 3) -> Direction:
    candles = data.reset_index(drop=True)
    if len(candles) < swing_length * 2 + 5:
        return _fallback_trend(candles)

    swing_highs: list[float] = []
    swing_lows: list[float] = []
    for index in range(swing_length, len(candles) - swing_length):
        high = float(candles.at[index, "high"])
        low = float(candles.at[index, "low"])
        if high == float(candles.loc[index - swing_length : index + swing_length, "high"].max()):
            swing_highs.append(high)
        if low == float(candles.loc[index - swing_length : index + swing_length, "low"].min()):
            swing_lows.append(low)

    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        if swing_highs[-1] > swing_highs[-2] and swing_lows[-1] > swing_lows[-2]:
            return "bullish"
        if swing_highs[-1] < swing_highs[-2] and swing_lows[-1] < swing_lows[-2]:
            return "bearish"
    return _fallback_trend(candles)


def _fallback_trend(candles: pd.DataFrame) -> Direction:
    first_close = float(candles.iloc[0]["close"])
    last_close = float(candles.iloc[-1]["close"])
    if last_close > first_close:
        return "bullish"
    if last_close < first_close:
        return "bearish"
    return "neutral"


def _find_zone_match(direction: Direction, timeframes: dict[str, pd.DataFrame], config: ConfluenceConfig):
    for name, data in timeframes.items():
        if data is None or data.empty:
            continue
        blocks = latest_active_order_blocks(data, candle_range=config.order_block_range)
        gaps = latest_unmitigated_fvgs(data, threshold_percent=config.fvg_threshold_percent)
        for block in reversed(blocks):
            if block.side != direction:
                continue
            for gap in reversed(gaps):
                if gap.side == direction and _zones_overlap(block.top, block.bottom, gap.top, gap.bottom):
                    return name, block, gap
    return None


def _find_recent_liquidity_sweep(direction: Direction, timeframes: dict[str, pd.DataFrame], config: ConfluenceConfig):
    desired_side = "sell_side" if direction == "bullish" else "buy_side"
    for name, data in timeframes.items():
        if data is None or data.empty:
            continue
        zones = detect_liquidity_zones(data, length=config.liquidity_length)
        crossed = [zone for zone in zones if zone.side == desired_side and zone.crossed]
        if crossed:
            return name, crossed[-1]
    return None


def _find_ltf_confirmation(direction: Direction, timeframes: dict[str, pd.DataFrame]) -> str | None:
    for name, data in timeframes.items():
        if data is None or data.empty:
            continue
        if _has_choch(direction, data):
            return f"{name} CHOCH / market structure shift confirmed"
        if _has_engulfing(direction, data):
            return f"{name} engulfing confirmation formed"
        gaps = latest_unmitigated_fvgs(data, limit=1)
        if gaps and gaps[-1].side == direction:
            return f"{name} FVG confirmation formed"
    return None


def _has_choch(direction: Direction, data: pd.DataFrame, lookback: int = 10) -> bool:
    candles = data.reset_index(drop=True)
    if len(candles) < lookback + 2:
        return False
    recent = candles.iloc[-lookback - 1 : -1]
    close = float(candles.iloc[-1]["close"])
    if direction == "bullish":
        return close > float(recent["high"].max())
    if direction == "bearish":
        return close < float(recent["low"].min())
    return False


def _has_engulfing(direction: Direction, data: pd.DataFrame) -> bool:
    candles = data.reset_index(drop=True)
    if len(candles) < 2:
        return False
    previous = candles.iloc[-2]
    current = candles.iloc[-1]
    po, pc = float(previous["open"]), float(previous["close"])
    co, cc = float(current["open"]), float(current["close"])
    if direction == "bullish":
        return pc < po and cc > co and cc > po and co < pc
    if direction == "bearish":
        return pc > po and cc < co and cc < po and co > pc
    return False


def _find_volume_spike(timeframes: dict[str, pd.DataFrame], config: ConfluenceConfig) -> str | None:
    for name, data in timeframes.items():
        if data is None or data.empty or "volume" not in data.columns or len(data) < config.volume_lookback + 1:
            continue
        candles = data.reset_index(drop=True)
        recent = float(candles.iloc[-1]["volume"])
        average = float(candles.iloc[-config.volume_lookback - 1 : -1]["volume"].mean())
        if average > 0 and recent >= average * config.volume_multiplier:
            return f"{name} volume spike confirmed"
    return None


def _zones_overlap(first_top: float, first_bottom: float, second_top: float, second_bottom: float) -> bool:
    return max(first_bottom, second_bottom) <= min(first_top, second_top)

