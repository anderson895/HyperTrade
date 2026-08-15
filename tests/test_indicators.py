"""Indicator maths, checked against hand-computed values."""

import pytest

from src.core.indicators import atr, crossed_above, crossed_below, ema, true_range
from src.core.models import Candle


def make_candle(high: float, low: float, close: float, open_: float | None = None) -> Candle:
    return Candle(
        open_time_ms=0,
        close_time_ms=1,
        open=close if open_ is None else open_,
        high=high,
        low=low,
        close=close,
        volume=1.0,
        trades=1,
    )


# --- EMA ------------------------------------------------------------------


def test_ema_matches_hand_computation():
    # period 3 -> multiplier 0.5, seeded with mean(1,2,3) = 2.0 at index 2.
    assert ema([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]


def test_ema_is_none_until_it_has_enough_history():
    assert ema([1, 2], 5) == [None, None]
    assert ema([1, 2, 3, 4, 5], 5)[:4] == [None] * 4


def test_ema_of_a_flat_series_is_that_value():
    assert ema([100.0] * 10, 4)[-1] == pytest.approx(100.0)


def test_ema_stays_aligned_with_its_input():
    values = list(range(50))
    assert len(ema(values, 21)) == len(values)


def test_ema_rejects_a_bad_period():
    with pytest.raises(ValueError):
        ema([1, 2, 3], 0)


# --- ATR ------------------------------------------------------------------


def test_true_range_uses_the_first_candles_own_range():
    assert true_range([make_candle(10, 8, 9)])[0] == 2


def test_true_range_counts_a_gap_from_the_previous_close():
    """A gap up is real risk even though the candle itself looks small."""
    candles = [make_candle(10, 8, 9), make_candle(20, 19, 19.5)]
    assert true_range(candles)[1] == pytest.approx(11.0)  # 20 - 9, not 20 - 19


def test_atr_of_constant_ranges_is_that_range():
    assert atr([make_candle(10, 8, 9)] * 20, 14)[-1] == pytest.approx(2.0)


def test_atr_is_none_during_warmup():
    assert atr([make_candle(10, 8, 9)] * 10, 14) == [None] * 10


def test_atr_smoothing_is_gradual_after_a_shock():
    """Wilder smoothing must not let one wide candle blow the stop distance out."""
    period = 14
    candles = [make_candle(10, 8, 9)] * 20 + [make_candle(40, 8, 30)]
    values = atr(candles, period)
    calm, shocked = values[19], values[20]
    spike = true_range(candles)[-1]  # 32 — sixteen times the calm range

    assert calm == pytest.approx(2.0)
    # Wilder moves about 1/period of the way toward the new range, so the stop
    # widens slightly instead of leaping to the spike.
    assert shocked == pytest.approx(calm + (spike - calm) / period)
    assert shocked < spike / 5


# --- crossovers -----------------------------------------------------------


def test_crossed_above_detects_the_crossing_bar_only():
    fast = [1.0, 3.0, 4.0]
    slow = [2.0, 2.0, 2.0]
    assert not crossed_above(fast, slow, 0)
    assert crossed_above(fast, slow, 1)
    assert not crossed_above(fast, slow, 2)  # already above, not a fresh cross


def test_crossed_below_is_the_mirror():
    fast = [3.0, 1.0]
    slow = [2.0, 2.0]
    assert crossed_below(fast, slow, 1)
    assert not crossed_above(fast, slow, 1)


def test_no_crossover_is_claimed_while_an_indicator_is_warming_up():
    """A None-to-value transition is warmup, not a signal."""
    assert not crossed_above([None, 3.0], [None, 2.0], 1)
    assert not crossed_above([1.0, 3.0], [None, 2.0], 1)
