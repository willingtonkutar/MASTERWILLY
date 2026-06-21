"""Fair value gap detection for the SMC strategy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd


FvgSide = Literal["bullish", "bearish"]


@dataclass(frozen=True)
class FairValueGap:
    side: FvgSide
    start_index: int
    detected_index: int
    top: float
    bottom: float
    mitigated: bool
    mitigated_index: int | None


def detect_fair_value_gaps(
    data: pd.DataFrame,
    threshold_percent: float = 0,
    auto_threshold: bool = False,
    include_mitigated: bool = True,
) -> list[FairValueGap]:
    _validate_input(data, threshold_percent)

    candles = data.reset_index(drop=True)
    gaps: list[FairValueGap] = []
    cumulative_range_ratio = 0.0

    for index in range(len(candles)):
        low = float(candles.at[index, "low"])
        high = float(candles.at[index, "high"])

        if low != 0:
            cumulative_range_ratio += (high - low) / low

        if index < 2:
            continue

        threshold = (
            cumulative_range_ratio / index
            if auto_threshold and index > 0
            else threshold_percent / 100
        )
        high_two_back = float(candles.at[index - 2, "high"])
        low_two_back = float(candles.at[index - 2, "low"])
        previous_close = float(candles.at[index - 1, "close"])

        bullish = (
            low > high_two_back
            and previous_close > high_two_back
            and (low - high_two_back) / high_two_back > threshold
        )
        bearish = (
            high < low_two_back
            and previous_close < low_two_back
            and (low_two_back - high) / high > threshold
        )

        if bullish:
            gap = FairValueGap("bullish", index - 2, index, low, high_two_back, False, None)
            gaps.append(_with_mitigation(candles, gap, index + 1))
        elif bearish:
            gap = FairValueGap("bearish", index - 2, index, low_two_back, high, False, None)
            gaps.append(_with_mitigation(candles, gap, index + 1))

    return gaps if include_mitigated else [gap for gap in gaps if not gap.mitigated]


def latest_unmitigated_fvgs(
    data: pd.DataFrame,
    threshold_percent: float = 0,
    auto_threshold: bool = False,
    limit: int = 0,
) -> list[FairValueGap]:
    gaps = detect_fair_value_gaps(
        data=data,
        threshold_percent=threshold_percent,
        auto_threshold=auto_threshold,
        include_mitigated=False,
    )
    return gaps if limit <= 0 else gaps[-limit:]


def _validate_input(data: pd.DataFrame, threshold_percent: float) -> None:
    missing = {"open", "high", "low", "close"}.difference(data.columns)
    if missing:
        raise ValueError(f"data is missing required columns: {', '.join(sorted(missing))}")
    if len(data) < 3:
        raise ValueError("data must contain at least 3 candles")
    if threshold_percent < 0:
        raise ValueError("threshold_percent must be greater than or equal to 0")


def _with_mitigation(candles: pd.DataFrame, gap: FairValueGap, start_index: int) -> FairValueGap:
    for candle_index in range(start_index, len(candles)):
        close = float(candles.at[candle_index, "close"])
        if gap.side == "bullish" and close < gap.bottom:
            return FairValueGap(gap.side, gap.start_index, gap.detected_index, gap.top, gap.bottom, True, candle_index)
        if gap.side == "bearish" and close > gap.top:
            return FairValueGap(gap.side, gap.start_index, gap.detected_index, gap.top, gap.bottom, True, candle_index)
    return gap

