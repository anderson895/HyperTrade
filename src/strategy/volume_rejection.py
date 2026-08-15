"""Volume rejection — a fakeout past the 24-hour range, punished on high volume.

The setup is a failed breakout. Price pushes past the highest high or lowest low of
the last 24 hours on volume well above normal, then gets pushed straight back inside
and closes there, leaving a long wick where it was rejected. The wick is the
evidence: buyers or sellers were there in size and lost.

This is a mean-reversion system, and the opposite of the trend follower beside it.
The two disagree by design — one takes the breakout, the other fades it — so only
one runs at a time.

Where the numbers came from: this implements a specification the user backtested
independently, and the periods, multiples and buffers below are theirs. They are
reproduced exactly rather than tuned, because a strategy that does not match the
backtest it was chosen from tells you nothing about what to expect.
"""

from __future__ import annotations

import logging
import statistics
from collections.abc import Sequence
from typing import Any

from ..core.models import Candle, Side, Signal
from .base import Strategy, register

log = logging.getLogger(__name__)


@register
class VolumeRejection(Strategy):
    name = "volume_rejection"
    display_name = "Volume rejection (24h range fakeout)"

    def __init__(
        self,
        range_lookback: int = 96,
        volume_lookback: int = 20,
        volume_multiple: float = 2.0,
        wick_fraction: float = 0.40,
        stop_buffer: float = 0.001,
        take_profit_rr: float = 2.0,
    ) -> None:
        """
        `range_lookback` — bars forming the range. 96 is 24 hours of 15m candles.

        `stop_buffer` — how far past the rejection wick the stop sits, as a fraction
        of the wick price. The wick is where the move already failed once; sitting
        exactly on it would be stopped out by a repeat of the same failure.
        """
        if range_lookback < 2 or volume_lookback < 2:
            raise ValueError("lookbacks must be at least 2 bars")
        if volume_multiple <= 0:
            raise ValueError("volume_multiple must be positive")
        if not 0 < wick_fraction <= 1:
            raise ValueError("wick_fraction must be between 0 and 1")
        if stop_buffer < 0:
            raise ValueError("stop_buffer cannot be negative")
        if take_profit_rr <= 0:
            raise ValueError("take_profit_rr must be positive")

        self.range_lookback = range_lookback
        self.volume_lookback = volume_lookback
        self.volume_multiple = volume_multiple
        self.wick_fraction = wick_fraction
        self.stop_buffer = stop_buffer
        self.take_profit_rr = take_profit_rr

    @property
    def warmup_candles(self) -> int:
        # The range needs its full window *before* the bar being judged, so one more
        # than the lookback. No EMA here to settle, so nothing beyond that.
        return max(self.range_lookback, self.volume_lookback) + 1

    def parameters(self) -> dict[str, Any]:
        return {
            "range_lookback": self.range_lookback,
            "volume_lookback": self.volume_lookback,
            "volume_multiple": self.volume_multiple,
            "wick_fraction": self.wick_fraction,
            "stop_buffer": self.stop_buffer,
            "take_profit_rr": self.take_profit_rr,
        }

    def typical_stop_distance(self, candles: Sequence[Candle]) -> float | None:
        """Roughly how far the stop would sit, for the pre-flight sizing check.

        A rejection candle's stop sits at the far extreme of the bar plus the
        buffer, so the distance from the close to the wider of the two wick tips is
        the honest estimate. Taken as a median over recent bars rather than the last
        one, which may be unusually quiet or unusually wild.
        """
        recent = candles[-self.volume_lookback:]
        if len(recent) < 2:
            return None
        reaches = [
            max(candle.high - candle.close, candle.close - candle.low)
            for candle in recent
        ]
        typical = statistics.median(reaches)
        return typical + candles[-1].close * self.stop_buffer if typical > 0 else None

    def evaluate(self, candles: Sequence[Candle]) -> Signal | None:
        if len(candles) < self.warmup_candles:
            return None

        bar = candles[-1]
        # The range is what price had to break *out of*, so the bar being judged is
        # excluded from it. Included, a new high would always be its own high and the
        # break could never be detected.
        window = candles[-(self.range_lookback + 1):-1]
        volumes = [c.volume for c in candles[-(self.volume_lookback + 1):-1]]

        average_volume = statistics.fmean(volumes)
        if average_volume <= 0 or bar.volume < average_volume * self.volume_multiple:
            return None

        length = bar.high - bar.low
        if length <= 0:
            return None  # a bar with no range has no wick to measure

        range_high = max(candle.high for candle in window)
        range_low = min(candle.low for candle in window)
        upper_wick = bar.high - max(bar.open, bar.close)
        lower_wick = min(bar.open, bar.close) - bar.low

        # Broke above, was pushed back, closed inside -> the breakout failed, so sell.
        if (
            bar.high >= range_high
            and upper_wick / length >= self.wick_fraction
            and bar.close < range_high
        ):
            side, wick = Side.SHORT, bar.high
        # The mirror image below the range.
        elif (
            bar.low <= range_low
            and lower_wick / length >= self.wick_fraction
            and bar.close > range_low
        ):
            side, wick = Side.LONG, bar.low
        else:
            return None

        entry = bar.close
        # Past the wick in the losing direction: above it for a short, below for a
        # long. `side.sign` is +1 for long and -1 for short, so subtracting it moves
        # the stop the right way for both without a branch.
        stop = wick * (1 - side.sign * self.stop_buffer)
        stop_distance = abs(entry - stop)
        if stop_distance <= 0 or stop <= 0:
            return None

        return Signal(
            side=side,
            entry_price=entry,
            stop_price=stop,
            take_profit_price=entry + side.sign * stop_distance * self.take_profit_rr,
            reason=(
                f"{'upper' if side is Side.SHORT else 'lower'} wick rejected the "
                f"{self.range_lookback}-bar "
                f"{'high' if side is Side.SHORT else 'low'} of "
                f"{range_high if side is Side.SHORT else range_low:.1f} on "
                f"{bar.volume / average_volume:.1f}x volume; wick is "
                f"{upper_wick / length if side is Side.SHORT else lower_wick / length:.0%} "
                f"of the bar, stop {stop_distance / entry:.2%} away, "
                f"target {self.take_profit_rr:g}R"
            ),
        )
