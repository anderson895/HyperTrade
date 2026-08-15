"""Trend following — EMA crossover with an ATR stop.

Chosen for HyperTrade because leverage changes which failure mode is survivable. A
mean-reversion system fading a move that keeps going holds a loser that grows; at 5x
that ends in liquidation. A trend system is wrong often but cheaply — the stop sits
close to entry and the winners are the ones that run.

The consequence to expect: **this loses more trades than it wins.** At a 2R target it
only needs about 34% of trades to work out to break even. A run of small losses is
the system operating normally, not a bug.

Two optional chop filters are available; both default to OFF. Neither is enabled
without backtest evidence on real candles that it improves expectancy — a filter
that removes more good trades than bad ones is worse than no filter, and that is the
usual outcome. Run `tools/run_backtest.py` to re-measure.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from ..core.indicators import atr, crossed_above, crossed_below, ema
from ..core.models import Candle, Side, Signal
from .base import Strategy, register

log = logging.getLogger(__name__)


@register
class TrendFollowing(Strategy):
    name = "trend_following"
    display_name = "Trend following (EMA crossover + ATR stop)"

    def __init__(
        self,
        fast_period: int = 21,
        slow_period: int = 55,
        atr_period: int = 14,
        atr_stop_multiple: float = 2.0,
        reward_risk: float = 2.0,
        slope_lookback: int = 0,
        trend_filter_period: int = 0,
    ) -> None:
        """
        `slope_lookback` — bars over which the slow EMA must be moving in the trade's
        direction. 0 disables it.

        `trend_filter_period` — only take longs above this EMA and shorts below it.
        0 disables it.
        """
        if fast_period < 1 or slow_period < 1 or atr_period < 1:
            raise ValueError("periods must be >= 1")
        if fast_period >= slow_period:
            raise ValueError(
                f"fast_period ({fast_period}) must be shorter than slow_period ({slow_period})"
            )
        if atr_stop_multiple <= 0 or reward_risk <= 0:
            raise ValueError("atr_stop_multiple and reward_risk must be positive")
        if slope_lookback < 0 or trend_filter_period < 0:
            raise ValueError("filter lengths cannot be negative")

        self.fast_period = fast_period
        self.slow_period = slow_period
        self.atr_period = atr_period
        self.atr_stop_multiple = atr_stop_multiple
        self.reward_risk = reward_risk
        self.slope_lookback = slope_lookback
        self.trend_filter_period = trend_filter_period

    @property
    def warmup_candles(self) -> int:
        # EMAs seeded from an SMA need several multiples of their period before they
        # settle onto the values a chart would show; 3x is the usual rule of thumb.
        longest = max(self.slow_period, self.atr_period, self.trend_filter_period)
        return longest * 3 + self.slope_lookback

    def parameters(self) -> dict[str, Any]:
        return {
            "fast_period": self.fast_period,
            "slow_period": self.slow_period,
            "atr_period": self.atr_period,
            "atr_stop_multiple": self.atr_stop_multiple,
            "reward_risk": self.reward_risk,
            "slope_lookback": self.slope_lookback,
            "trend_filter_period": self.trend_filter_period,
        }

    def evaluate(self, candles: Sequence[Candle]) -> Signal | None:
        # Indicators are recomputed over the whole window on every closed candle.
        # That is O(n) per evaluation at most once per 5 minutes — far cheaper than
        # the incremental state it would take to avoid, and impossible to desync.
        if len(candles) < self.warmup_candles:
            return None

        closes = [candle.close for candle in candles]
        fast = ema(closes, self.fast_period)
        slow = ema(closes, self.slow_period)
        ranges = atr(candles, self.atr_period)

        last = len(candles) - 1
        current_atr = ranges[last]
        if not current_atr or current_atr <= 0:
            return None

        if crossed_above(fast, slow, last):
            side = Side.LONG
        elif crossed_below(fast, slow, last):
            side = Side.SHORT
        else:
            return None

        veto = self._veto(closes, slow, last, side)
        if veto:
            log.debug("%s crossover skipped: %s", side.value, veto)
            return None

        entry = closes[last]
        stop_distance = current_atr * self.atr_stop_multiple
        stop = entry - side.sign * stop_distance
        if stop <= 0:
            return None

        return Signal(
            side=side,
            entry_price=entry,
            stop_price=stop,
            take_profit_price=entry + side.sign * stop_distance * self.reward_risk,
            reason=(
                f"EMA{self.fast_period} crossed "
                f"{'above' if side is Side.LONG else 'below'} EMA{self.slow_period}; "
                f"ATR({self.atr_period})={current_atr:.2f}, "
                f"stop {self.atr_stop_multiple:g}xATR, target {self.reward_risk:g}R"
            ),
        )

    def _veto(
        self,
        closes: Sequence[float],
        slow: Sequence[float | None],
        index: int,
        side: Side,
    ) -> str | None:
        """Reason to skip an otherwise valid crossover, or None to take it."""
        if self.slope_lookback:
            earlier = index - self.slope_lookback
            if earlier < 0 or slow[earlier] is None or slow[index] is None:
                return "slow EMA slope unavailable"
            if (slow[index] - slow[earlier]) * side.sign <= 0:
                return f"slow EMA is not moving {side.value} over {self.slope_lookback} bars"

        if self.trend_filter_period:
            trend = ema(closes, self.trend_filter_period)
            if trend[index] is None:
                return "trend EMA unavailable"
            if (closes[index] - trend[index]) * side.sign <= 0:
                return f"price is on the wrong side of EMA{self.trend_filter_period}"

        return None
