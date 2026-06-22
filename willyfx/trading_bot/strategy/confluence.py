"""SMC confluence engine.

Combines higher-timeframe trend, H1/M15 order block + FVG overlap,
liquidity sweeps, lower-timeframe confirmation, and volume into a scored
BUY/SELL/NO_TRADE decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import pandas as pd

from strategy.fair_value_gaps import FairValueGap, detect_fair_value_gaps, latest_unmitigated_fvgs
from strategy.liquidity import LiquidityZone, detect_liquidity_zones
from strategy.order_blocks import OrderBlock, latest_active_order_blocks


Direction = Literal["bullish", "bearish", "neutral"]
TradeAction = Literal["BUY", "SELL", "NO_TRADE"]


@dataclass(frozen=True)
class ConfluenceConfig:
    minimum_score: float = 8
    trend_score: float = 2
    htf_fvg_zone_score: float = 2.5
    fresh_order_block_score: float = 2.5
    mitigated_order_block_score: float = 1.0
    bos_order_block_score: float = 1.0
    fvg_overlap_score: float = 2.0
    ob_fvg_in_front_score: float = 3.0
    nested_zone_score: float = 2.0
    premium_discount_score: float = 1.0
    liquidity_sweep_score: float = 4.0
    mss_confirmation_score: float = 4.0
    ifvg_confirmation_score: float = 3.0
    engulfing_confirmation_score: float = 1.5
    choch_retest_score: float = 1.5
    volume_spike_score: float = 1.0
    order_block_range: int = 25
    liquidity_length: int = 14
    fvg_threshold_percent: float = 0
    volume_lookback: int = 20
    volume_multiplier: float = 1.5
    zone_tap_lookback: int = 5
    liquidity_sweep_lookback: int = 8
    entry_confirmation_lookback: int = 12
    mitigated_ob_min_entry_score: float = 5.0


@dataclass(frozen=True)
class ConfluenceDecision:
    action: TradeAction
    direction: Direction
    score: float
    reasons: list[str]
    order_block: OrderBlock | None = None
    fair_value_gap: FairValueGap | None = None
    liquidity_zone: LiquidityZone | None = None
    details: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ZoneCandidate:
    timeframe: str
    kind: Literal["FVG", "Order block"]
    side: Direction
    top: float
    bottom: float
    tapped: bool
    score: float
    reason: str
    order_block: OrderBlock | None = None
    fair_value_gap: FairValueGap | None = None
    has_fvg_overlap: bool = False
    has_fvg_in_front: bool = False
    nested_inside: tuple[str, ...] = ()


@dataclass(frozen=True)
class EntryConfirmation:
    timeframe: str
    kind: Literal["MSS", "iFVG", "Engulfing", "CHOCH retest"]
    score: float
    reason: str


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
    if bias == "bullish":
        best = buy_decision
    elif bias == "bearish":
        best = sell_decision
    else:
        best = buy_decision if buy_decision.score >= sell_decision.score else sell_decision

    if (
        bias == "neutral"
        or best.score < config.minimum_score
        or not best.details.get("zone_tapped")
        or not best.details.get("entry_confirmed")
        or best.details.get("mitigated_ob_blocked")
    ):
        reasons = list(best.reasons)
        if bias == "neutral":
            reasons.append("Higher-timeframe bias is neutral")
        if not best.details.get("zone_tapped"):
            reasons.append("Waiting for price to tap or trade inside a valid POI")
        if not best.details.get("entry_confirmed"):
            reasons.append("Waiting for 1M/2M/3M/5M entry confirmation")
        if best.details.get("mitigated_ob_blocked"):
            reasons.append("Mitigated order block did not meet A+ confirmation rules")
        if best.score < config.minimum_score:
            reasons.append(f"Score below minimum {config.minimum_score}")
        return ConfluenceDecision(
            action="NO_TRADE",
            direction=best.direction if bias != "neutral" else "neutral",
            score=best.score if bias != "neutral" else 0,
            reasons=reasons,
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
        if data is None or data.empty:
            continue
        blocks = _safe_latest_order_blocks(data, config=config)
        gaps = _safe_latest_fvgs(data, config=config)
        if not blocks and not gaps:
            continue
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

    zone_stack = _find_zone_stack(direction, zone_timeframes, config)
    watched_zones = [_zone_to_dict(zone) for zone in zone_stack]
    tapped_zones = [zone for zone in zone_stack if zone.tapped]
    details["watched_zones"] = watched_zones
    details["zone_tapped"] = bool(tapped_zones)

    if tapped_zones:
        zone_score = sum(zone.score for zone in tapped_zones)
        score += zone_score
        best_zone = max(tapped_zones, key=lambda zone: zone.score)
        selected_ob = best_zone.order_block
        selected_fvg = best_zone.fair_value_gap
        reasons.append(f"Price tapped POI stack worth {zone_score:.1f} points")
        reasons.extend(zone.reason for zone in tapped_zones[:6])
        details["zone_score"] = zone_score
        details["primary_zone"] = _zone_to_dict(best_zone)
        details["uses_mitigated_ob"] = bool(best_zone.order_block and best_zone.order_block.mitigated)
    elif zone_stack:
        reasons.append("Valid POIs found but price has not tapped them yet")
        reasons.extend(zone.reason for zone in zone_stack[:4])

    liquidity_match = _find_recent_liquidity_sweep(direction, zone_timeframes, config)
    if liquidity_match:
        timeframe, selected_liquidity = liquidity_match
        score += config.liquidity_sweep_score
        reasons.append(f"{timeframe} liquidity sweep detected")
        details["liquidity_timeframe"] = timeframe

    entry_confirmation = _find_ltf_confirmation(direction, entry_timeframes, config)
    details["entry_confirmed"] = bool(entry_confirmation)
    if entry_confirmation:
        score += entry_confirmation.score
        reasons.append(entry_confirmation.reason)
        details["entry_confirmation"] = {
            "timeframe": entry_confirmation.timeframe,
            "type": entry_confirmation.kind,
            "score": entry_confirmation.score,
        }

    if details.get("uses_mitigated_ob") and (
        not selected_liquidity or not entry_confirmation or entry_confirmation.score < config.mitigated_ob_min_entry_score
    ):
        details["mitigated_ob_blocked"] = True
        reasons.append("Mitigated OB requires A+ liquidity sweep plus strong entry confirmation")

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


def _find_zone_stack(
    direction: Direction,
    timeframes: dict[str, pd.DataFrame],
    config: ConfluenceConfig,
) -> list[ZoneCandidate]:
    raw_zones: list[ZoneCandidate] = []
    fvg_by_timeframe: dict[str, list[FairValueGap]] = {}

    for name, data in timeframes.items():
        if data is None or data.empty:
            continue

        gaps = _safe_latest_fvgs(data, config=config, limit=6)
        blocks = _safe_latest_order_blocks(data, config=config, limit=6)
        fvg_by_timeframe[name] = gaps

        for gap in gaps:
            if gap.side != direction:
                continue
            raw_zones.append(
                ZoneCandidate(
                    timeframe=name,
                    kind="FVG",
                    side=direction,
                    top=gap.top,
                    bottom=gap.bottom,
                    tapped=_recently_tapped(data, gap.top, gap.bottom, config.zone_tap_lookback),
                    score=_timeframe_weight(name, config.htf_fvg_zone_score),
                    reason=f"{name} {direction} FVG zone found at {gap.bottom:.2f}-{gap.top:.2f}",
                    fair_value_gap=gap,
                )
            )

        for block in blocks:
            if block.side != direction:
                continue

            same_tf_gaps = [gap for gap in gaps if gap.side == direction]
            overlap = next((gap for gap in reversed(same_tf_gaps) if _zones_overlap(block.top, block.bottom, gap.top, gap.bottom)), None)
            in_front = next((gap for gap in reversed(same_tf_gaps) if _fvg_in_front_of_ob(direction, block, gap)), None)

            base_score = config.mitigated_order_block_score if block.mitigated else config.fresh_order_block_score
            score = _timeframe_weight(name, base_score)
            reason_bits = [
                f"{name} {'mitigated' if block.mitigated else 'fresh'} {direction} OB at {block.bottom:.2f}-{block.top:.2f}"
            ]

            if block.break_level:
                score += config.bos_order_block_score
                reason_bits.append("led to BOS")
            if overlap:
                score += config.fvg_overlap_score
                reason_bits.append("overlaps FVG")
            if in_front:
                score += config.ob_fvg_in_front_score
                reason_bits.append("has FVG in front")

            raw_zones.append(
                ZoneCandidate(
                    timeframe=name,
                    kind="Order block",
                    side=direction,
                    top=block.top,
                    bottom=block.bottom,
                    tapped=_recently_tapped(data, block.top, block.bottom, config.zone_tap_lookback),
                    score=score,
                    reason=", ".join(reason_bits),
                    order_block=block,
                    fair_value_gap=overlap or in_front,
                    has_fvg_overlap=bool(overlap),
                    has_fvg_in_front=bool(in_front),
                )
            )

    stacked: list[ZoneCandidate] = []
    for zone in raw_zones:
        nested_inside = tuple(
            parent.timeframe
            for parent in raw_zones
            if parent is not zone
            and parent.side == zone.side
            and _is_higher_timeframe(parent.timeframe, zone.timeframe)
            and _zone_inside(zone.top, zone.bottom, parent.top, parent.bottom)
        )
        score = zone.score + (len(nested_inside) * config.nested_zone_score)
        score += _premium_discount_bonus(direction, zone_timeframe_data=timeframes.get(zone.timeframe), config=config)
        reason = zone.reason
        if nested_inside:
            reason = f"{reason}, nested inside {'/'.join(nested_inside)} zone"
        stacked.append(
            ZoneCandidate(
                timeframe=zone.timeframe,
                kind=zone.kind,
                side=zone.side,
                top=zone.top,
                bottom=zone.bottom,
                tapped=zone.tapped,
                score=score,
                reason=reason,
                order_block=zone.order_block,
                fair_value_gap=zone.fair_value_gap,
                has_fvg_overlap=zone.has_fvg_overlap,
                has_fvg_in_front=zone.has_fvg_in_front,
                nested_inside=nested_inside,
            )
        )

    return sorted(stacked, key=lambda zone: (not zone.tapped, -zone.score, _timeframe_rank(zone.timeframe)))


def _find_recent_liquidity_sweep(direction: Direction, timeframes: dict[str, pd.DataFrame], config: ConfluenceConfig):
    desired_side = "sell_side" if direction == "bullish" else "buy_side"
    for name, data in timeframes.items():
        if data is None or data.empty:
            continue
        zones = _safe_liquidity_zones(data, config)
        recent_zones = [zone for zone in zones if zone.side == desired_side]
        candles = data.reset_index(drop=True)
        for zone in reversed(recent_zones):
            start = max(zone.detected_index + 1, len(candles) - config.liquidity_sweep_lookback)
            for candle_index in range(start, len(candles)):
                candle = candles.loc[candle_index]
                low = float(candle["low"])
                high = float(candle["high"])
                close = float(candle["close"])
                open_ = float(candle["open"])
                if direction == "bullish" and low < zone.bottom and close > zone.bottom and close >= open_:
                    return name, zone
                if direction == "bearish" and high > zone.top and close < zone.top and close <= open_:
                    return name, zone
    return None


def _find_ltf_confirmation(
    direction: Direction,
    timeframes: dict[str, pd.DataFrame],
    config: ConfluenceConfig,
) -> EntryConfirmation | None:
    confirmations: list[EntryConfirmation] = []

    for name, data in timeframes.items():
        if data is None or data.empty:
            continue

        tf_bonus = _entry_timeframe_bonus(name)

        retest = _has_choch_retest(direction, data, lookback=config.entry_confirmation_lookback)
        if retest:
            confirmations.append(
                EntryConfirmation(
                    name,
                    "CHOCH retest",
                    config.mss_confirmation_score + config.choch_retest_score + tf_bonus,
                    f"{name} CHOCH formed and retested in {direction} direction",
                )
            )

        if _has_choch(direction, data, lookback=config.entry_confirmation_lookback):
            confirmations.append(
                EntryConfirmation(
                    name,
                    "MSS",
                    config.mss_confirmation_score + tf_bonus,
                    f"{name} market structure shift / CHOCH confirmed",
                )
            )

        if _has_inverse_fvg(direction, data):
            confirmations.append(
                EntryConfirmation(
                    name,
                    "iFVG",
                    config.ifvg_confirmation_score + tf_bonus,
                    f"{name} inverse FVG confirmation formed",
                )
            )

        if _has_engulfing(direction, data):
            confirmations.append(
                EntryConfirmation(
                    name,
                    "Engulfing",
                    config.engulfing_confirmation_score + tf_bonus,
                    f"{name} engulfing candle confirmed",
                )
            )

    if not confirmations:
        return None

    return max(confirmations, key=lambda item: item.score)


def _has_choch(direction: Direction, data: pd.DataFrame, lookback: int = 10) -> bool:
    candles = data.reset_index(drop=True)
    if len(candles) < lookback + 2:
        return False
    previous_structure = _infer_trend(candles.iloc[:-1], swing_length=2)
    recent = candles.iloc[-lookback - 1 : -1]
    close = float(candles.iloc[-1]["close"])
    if direction == "bullish":
        return previous_structure != "bullish" and close > float(recent["high"].max())
    if direction == "bearish":
        return previous_structure != "bearish" and close < float(recent["low"].min())
    return False


def _has_choch_retest(direction: Direction, data: pd.DataFrame, lookback: int = 12) -> bool:
    candles = data.reset_index(drop=True)
    if len(candles) < lookback + 4:
        return False

    start = len(candles) - lookback
    for break_index in range(start, len(candles) - 1):
        prior = candles.iloc[max(0, break_index - lookback) : break_index]
        if prior.empty:
            continue

        break_close = float(candles.iloc[break_index]["close"])
        if direction == "bullish":
            break_level = float(prior["high"].max())
            if break_close <= break_level:
                continue
            retest = candles.iloc[break_index + 1 :]
            if any(float(row["low"]) <= break_level <= float(row["high"]) and float(row["close"]) > break_level for _, row in retest.iterrows()):
                return True
        elif direction == "bearish":
            break_level = float(prior["low"].min())
            if break_close >= break_level:
                continue
            retest = candles.iloc[break_index + 1 :]
            if any(float(row["low"]) <= break_level <= float(row["high"]) and float(row["close"]) < break_level for _, row in retest.iterrows()):
                return True

    return False


def _has_inverse_fvg(direction: Direction, data: pd.DataFrame) -> bool:
    candles = data.reset_index(drop=True)
    gaps = _safe_latest_fvgs(candles, config=ConfluenceConfig(), limit=5, include_mitigated=True)
    if not gaps or candles.empty:
        return False

    last_close = float(candles.iloc[-1]["close"])
    last_open = float(candles.iloc[-1]["open"])
    body_high = max(last_open, last_close)
    body_low = min(last_open, last_close)

    for gap in reversed(gaps):
        if direction == "bullish" and gap.side == "bearish" and body_low > gap.top:
            return True
        if direction == "bearish" and gap.side == "bullish" and body_high < gap.bottom:
            return True
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


def _safe_latest_order_blocks(data: pd.DataFrame, config: ConfluenceConfig, limit: int = 0) -> list[OrderBlock]:
    try:
        return latest_active_order_blocks(data, candle_range=config.order_block_range, limit=limit)
    except (ValueError, KeyError, TypeError):
        return []


def _safe_latest_fvgs(
    data: pd.DataFrame,
    config: ConfluenceConfig,
    limit: int = 0,
    include_mitigated: bool = False,
) -> list[FairValueGap]:
    try:
        if include_mitigated:
            gaps = detect_fair_value_gaps(data, threshold_percent=config.fvg_threshold_percent, include_mitigated=True)
            return gaps if limit <= 0 else gaps[-limit:]
        return latest_unmitigated_fvgs(data, threshold_percent=config.fvg_threshold_percent, limit=limit)
    except (ValueError, KeyError, TypeError):
        return []


def _safe_liquidity_zones(data: pd.DataFrame, config: ConfluenceConfig) -> list[LiquidityZone]:
    try:
        return detect_liquidity_zones(data, length=config.liquidity_length)
    except (ValueError, KeyError, TypeError):
        return []


def _recently_tapped(data: pd.DataFrame, top: float, bottom: float, lookback: int) -> bool:
    candles = data.reset_index(drop=True).tail(max(1, lookback))
    for _, candle in candles.iterrows():
        high = float(candle["high"])
        low = float(candle["low"])
        close = float(candle["close"])
        if _price_in_zone(close, top, bottom) or (low <= top and high >= bottom):
            return True
    return False


def _price_in_zone(price: float, top: float, bottom: float) -> bool:
    return bottom <= price <= top


def _zone_to_dict(zone: ZoneCandidate) -> dict[str, object]:
    return {
        "timeframe": zone.timeframe,
        "type": zone.kind,
        "side": zone.side,
        "top": zone.top,
        "bottom": zone.bottom,
        "tapped": zone.tapped,
        "score": zone.score,
        "has_fvg_overlap": zone.has_fvg_overlap,
        "has_fvg_in_front": zone.has_fvg_in_front,
        "nested_inside": list(zone.nested_inside),
        "mitigated": bool(zone.order_block and zone.order_block.mitigated),
    }


def _timeframe_rank(timeframe: str) -> int:
    order = {"W1": 70, "D1": 60, "H4": 50, "H1": 40, "M15": 30, "M5": 20, "M3": 13, "M2": 12, "M1": 11}
    return order.get(str(timeframe).upper(), 0)


def _is_higher_timeframe(parent: str, child: str) -> bool:
    return _timeframe_rank(parent) > _timeframe_rank(child)


def _timeframe_weight(timeframe: str, base_score: float) -> float:
    tf = str(timeframe).upper()
    if tf == "H1":
        return base_score + 0.5
    if tf == "M15":
        return base_score
    if tf == "M5":
        return max(base_score - 0.5, 0.5)
    return base_score


def _entry_timeframe_bonus(timeframe: str) -> float:
    tf = str(timeframe).upper()
    if tf == "M5":
        return 0.75
    if tf == "M3":
        return 0.5
    if tf == "M2":
        return 0.25
    return 0.0


def _zone_inside(child_top: float, child_bottom: float, parent_top: float, parent_bottom: float) -> bool:
    return child_top <= parent_top and child_bottom >= parent_bottom


def _fvg_in_front_of_ob(direction: Direction, block: OrderBlock, gap: FairValueGap) -> bool:
    if gap.side != direction:
        return False
    block_height = max(block.top - block.bottom, 0.01)
    if _zones_overlap(block.top, block.bottom, gap.top, gap.bottom):
        return True
    if direction == "bullish":
        return block.bottom <= gap.bottom <= block.top + (block_height * 2.0)
    if direction == "bearish":
        return block.bottom - (block_height * 2.0) <= gap.top <= block.top
    return False


def _premium_discount_bonus(
    direction: Direction,
    zone_timeframe_data: pd.DataFrame | None,
    config: ConfluenceConfig,
) -> float:
    if zone_timeframe_data is None or zone_timeframe_data.empty:
        return 0.0

    candles = zone_timeframe_data.reset_index(drop=True).tail(80)
    if len(candles) < 10:
        return 0.0

    swing_high = float(candles["high"].max())
    swing_low = float(candles["low"].min())
    midpoint = (swing_high + swing_low) / 2
    last_close = float(candles.iloc[-1]["close"])

    if direction == "bullish" and last_close <= midpoint:
        return config.premium_discount_score
    if direction == "bearish" and last_close >= midpoint:
        return config.premium_discount_score
    return 0.0


def _zones_overlap(first_top: float, first_bottom: float, second_top: float, second_bottom: float) -> bool:
    return max(first_bottom, second_bottom) <= min(first_top, second_top)

