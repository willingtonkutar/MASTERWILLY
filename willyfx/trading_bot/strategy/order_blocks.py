"""Order block detection for the SMC strategy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd


OrderBlockSide = Literal["bullish", "bearish"]


@dataclass(frozen=True)
class OrderBlock:
    side: OrderBlockSide
    candle_index: int
    created_index: int
    top: float
    bottom: float
    break_level: float
    mitigated: bool
    mitigated_index: int | None
    invalidated: bool
    invalidated_index: int | None


def detect_order_blocks(
    data: pd.DataFrame,
    candle_range: int = 25,
    include_mitigated: bool = True,
    include_invalidated: bool = False,
) -> list[OrderBlock]:
    _validate_input(data, candle_range)
    candles = data.reset_index(drop=True)

    last_down_index = 0
    last_down = 0.0
    last_low = float("inf")
    last_up_index = 0
    last_up_low = 0.0
    last_high = 0.0
    last_long_index = 0

    long_blocks: list[OrderBlock] = []
    short_blocks: list[OrderBlock] = []
    completed: list[OrderBlock] = []

    for index in range(len(candles)):
        open_ = float(candles.at[index, "open"])
        high = float(candles.at[index, "high"])
        low = float(candles.at[index, "low"])
        close = float(candles.at[index, "close"])

        if index > candle_range:
            structure_low = float(candles.loc[index - candle_range : index - 1, "low"].min())
            previous_close = float(candles.at[index - 1, "close"])

            if previous_close >= structure_low and close < structure_low and (index - last_up_index) < 1000:
                short_blocks.append(
                    OrderBlock("bearish", last_up_index, index, last_high, last_up_low, structure_low, False, None, False, None)
                )

            next_short_blocks: list[OrderBlock] = []
            for block in short_blocks:
                updated = block
                if high > block.bottom and low < block.bottom and index > block.created_index and not block.mitigated:
                    updated = _replace_block(block, mitigated=True, mitigated_index=index)

                if close > block.top:
                    completed.append(_replace_block(updated, invalidated=True, invalidated_index=index))
                    if (index - last_down_index) < 1000 and index > last_long_index:
                        long_blocks.append(
                            OrderBlock("bullish", last_down_index, index, last_down, last_low, block.top, False, None, False, None)
                        )
                        last_long_index = index
                else:
                    next_short_blocks.append(updated)
            short_blocks = next_short_blocks

            next_long_blocks: list[OrderBlock] = []
            for block in long_blocks:
                updated = block
                if low <= block.top and high > block.top and index > block.created_index and not block.mitigated:
                    updated = _replace_block(block, mitigated=True, mitigated_index=index)

                if close < block.bottom:
                    completed.append(_replace_block(updated, invalidated=True, invalidated_index=index))
                else:
                    next_long_blocks.append(updated)
            long_blocks = next_long_blocks

        if close < open_:
            last_down = high
            last_down_index = index
            last_low = low

        if close > open_:
            last_up_index = index
            last_up_low = low
            last_high = high

        last_high = max(high, last_high)
        last_low = min(low, last_low)

    blocks = sorted(completed + long_blocks + short_blocks, key=lambda block: block.created_index)
    if not include_mitigated:
        blocks = [block for block in blocks if not block.mitigated]
    if not include_invalidated:
        blocks = [block for block in blocks if not block.invalidated]
    return blocks


def latest_active_order_blocks(data: pd.DataFrame, candle_range: int = 25, limit: int = 0) -> list[OrderBlock]:
    blocks = detect_order_blocks(data, candle_range=candle_range, include_mitigated=True, include_invalidated=False)
    return blocks if limit <= 0 else blocks[-limit:]


def _replace_block(
    block: OrderBlock,
    mitigated: bool | None = None,
    mitigated_index: int | None = None,
    invalidated: bool | None = None,
    invalidated_index: int | None = None,
) -> OrderBlock:
    return OrderBlock(
        block.side,
        block.candle_index,
        block.created_index,
        block.top,
        block.bottom,
        block.break_level,
        block.mitigated if mitigated is None else mitigated,
        block.mitigated_index if mitigated_index is None else mitigated_index,
        block.invalidated if invalidated is None else invalidated,
        block.invalidated_index if invalidated_index is None else invalidated_index,
    )


def _validate_input(data: pd.DataFrame, candle_range: int) -> None:
    missing = {"open", "high", "low", "close"}.difference(data.columns)
    if missing:
        raise ValueError(f"data is missing required columns: {', '.join(sorted(missing))}")
    if candle_range < 5:
        raise ValueError("candle_range must be at least 5")
    if len(data) <= candle_range:
        raise ValueError("data does not contain enough candles for this candle_range")

