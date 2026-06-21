"""Liquidity swing zone detection for the SMC strategy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd


SwingArea = Literal["wick_extremity", "full_range"]
LiquiditySide = Literal["buy_side", "sell_side"]
FilterBy = Literal["count", "volume"]


@dataclass(frozen=True)
class LiquidityZone:
    side: LiquiditySide
    pivot_index: int
    detected_index: int
    top: float
    bottom: float
    level: float
    interaction_count: int
    interaction_volume: float
    crossed: bool
    crossed_index: int | None


def detect_liquidity_zones(
    data: pd.DataFrame,
    length: int = 14,
    area: SwingArea = "wick_extremity",
    filter_by: FilterBy = "count",
    filter_value: float = 0,
    include_buy_side: bool = True,
    include_sell_side: bool = True,
) -> list[LiquidityZone]:
    _validate_input(data, length, area, filter_by)
    candles = data.reset_index(drop=True)
    zones: list[LiquidityZone] = []

    for pivot_index in range(length, len(candles) - length):
        detected_index = pivot_index + length

        if include_buy_side and _is_pivot_high(candles, pivot_index, length):
            zone = _build_zone(candles, "buy_side", pivot_index, detected_index, area)
            zone = _with_interactions(candles, zone, detected_index + 1)
            if _passes_filter(zone, filter_by, filter_value):
                zones.append(zone)

        if include_sell_side and _is_pivot_low(candles, pivot_index, length):
            zone = _build_zone(candles, "sell_side", pivot_index, detected_index, area)
            zone = _with_interactions(candles, zone, detected_index + 1)
            if _passes_filter(zone, filter_by, filter_value):
                zones.append(zone)

    return sorted(zones, key=lambda zone: zone.detected_index)


def _validate_input(data: pd.DataFrame, length: int, area: SwingArea, filter_by: FilterBy) -> None:
    missing = {"open", "high", "low", "close", "volume"}.difference(data.columns)
    if missing:
        raise ValueError(f"data is missing required columns: {', '.join(sorted(missing))}")
    if length < 1:
        raise ValueError("length must be greater than 0")
    if len(data) < length * 2 + 1:
        raise ValueError("data does not contain enough candles for this length")
    if area not in ("wick_extremity", "full_range"):
        raise ValueError("area must be 'wick_extremity' or 'full_range'")
    if filter_by not in ("count", "volume"):
        raise ValueError("filter_by must be 'count' or 'volume'")


def _is_pivot_high(candles: pd.DataFrame, pivot_index: int, length: int) -> bool:
    pivot_high = candles.at[pivot_index, "high"]
    window = candles.loc[pivot_index - length : pivot_index + length, "high"]
    return bool(pivot_high == window.max())


def _is_pivot_low(candles: pd.DataFrame, pivot_index: int, length: int) -> bool:
    pivot_low = candles.at[pivot_index, "low"]
    window = candles.loc[pivot_index - length : pivot_index + length, "low"]
    return bool(pivot_low == window.min())


def _build_zone(
    candles: pd.DataFrame,
    side: LiquiditySide,
    pivot_index: int,
    detected_index: int,
    area: SwingArea,
) -> LiquidityZone:
    pivot = candles.loc[pivot_index]

    if side == "buy_side":
        top = float(pivot["high"])
        bottom = max(float(pivot["close"]), float(pivot["open"])) if area == "wick_extremity" else float(pivot["low"])
        level = top
    else:
        top = min(float(pivot["close"]), float(pivot["open"])) if area == "wick_extremity" else float(pivot["high"])
        bottom = float(pivot["low"])
        level = bottom

    return LiquidityZone(side, pivot_index, detected_index, top, bottom, level, 0, 0.0, False, None)


def _with_interactions(candles: pd.DataFrame, zone: LiquidityZone, start_index: int) -> LiquidityZone:
    count = 0
    volume = 0.0
    crossed = False
    crossed_index: int | None = None

    for candle_index in range(start_index, len(candles)):
        candle = candles.loc[candle_index]
        if candle["low"] < zone.top and candle["high"] > zone.bottom:
            count += 1
            volume += float(candle["volume"])

        if zone.side == "buy_side" and float(candle["close"]) > zone.top:
            crossed = True
            crossed_index = candle_index
            break

        if zone.side == "sell_side" and float(candle["close"]) < zone.bottom:
            crossed = True
            crossed_index = candle_index
            break

    return LiquidityZone(
        zone.side,
        zone.pivot_index,
        zone.detected_index,
        zone.top,
        zone.bottom,
        zone.level,
        count,
        volume,
        crossed,
        crossed_index,
    )


def _passes_filter(zone: LiquidityZone, filter_by: FilterBy, filter_value: float) -> bool:
    target = zone.interaction_count if filter_by == "count" else zone.interaction_volume
    return target > filter_value

