"""Technical indicators — pure functions over sequences, no state and no I/O.

Every function returns a list the same length as its input, with `None` for the
leading positions where the indicator is not yet defined. Keeping the alignment
means an index into the candles is always the same index into the indicator, which
removes the off-by-one that quietly shifts a signal one bar into the future.
"""

from __future__ import annotations

from collections.abc import Sequence

from .models import Candle


def ema(values: Sequence[float], period: int) -> list[float | None]:
    """Exponential moving average, seeded with the simple average of the first
    `period` values (the conventional seeding, and what TradingView shows)."""
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")

    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out

    multiplier = 2.0 / (period + 1)
    current = sum(values[:period]) / period
    out[period - 1] = current

    for index in range(period, len(values)):
        current = values[index] * multiplier + current * (1 - multiplier)
        out[index] = current

    return out


def true_range(candles: Sequence[Candle]) -> list[float]:
    """True range per candle, which counts the gap from the previous close."""
    ranges: list[float] = []
    for index, candle in enumerate(candles):
        if index == 0:
            ranges.append(candle.high - candle.low)
            continue
        previous_close = candles[index - 1].close
        ranges.append(
            max(
                candle.high - candle.low,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )
        )
    return ranges


def atr(candles: Sequence[Candle], period: int = 14) -> list[float | None]:
    """Average true range with Wilder's smoothing — the standard ATR.

    Wilder's smoothing, not a plain moving average: it reacts more slowly, so the
    stop distance does not lurch after one wide candle.
    """
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")

    ranges = true_range(candles)
    out: list[float | None] = [None] * len(candles)
    if len(candles) < period:
        return out

    current = sum(ranges[:period]) / period
    out[period - 1] = current

    for index in range(period, len(candles)):
        current = (current * (period - 1) + ranges[index]) / period
        out[index] = current

    return out


def crossed_above(fast: Sequence[float | None], slow: Sequence[float | None], index: int) -> bool:
    """Whether `fast` crossed above `slow` on the bar at `index`.

    Requires both series to be defined on this bar and the one before it, so a
    crossover is never inferred from the moment an indicator warms up.
    """
    if index < 1:
        return False
    previous_fast, previous_slow = fast[index - 1], slow[index - 1]
    current_fast, current_slow = fast[index], slow[index]
    if None in (previous_fast, previous_slow, current_fast, current_slow):
        return False
    return previous_fast <= previous_slow and current_fast > current_slow


def crossed_below(fast: Sequence[float | None], slow: Sequence[float | None], index: int) -> bool:
    return crossed_above(slow, fast, index)
