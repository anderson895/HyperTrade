"""Bar-by-bar backtest over closed candles.

Results are reported in **R multiples** — profit measured in units of the risk taken
— because that is the one figure independent of account size, leverage, and the risk
setting. A strategy with an expectancy of +0.2R makes 0.2 x risk-per-trade on
average, whether the user risks 5 USDC or 500.

Known optimism, in the order it matters:

1. **Stops are assumed to fill exactly at the stop price.** Real stops gap through,
   especially on the 4h/daily/weekly timeframes and around news. Losses will be
   larger than modelled.
2. **When a candle contains both the stop and the target, the stop is taken.** The
   candle does not record which came first, so the pessimistic reading is used.
3. **Entries fill at the signal candle's close** with no slippage; `fee_bps` covers
   round-trip exchange fees only.

So treat the numbers as an upper bound, not a forecast.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from .core.models import Candle, Side
from .strategy.base import Strategy

log = logging.getLogger(__name__)

#: Hyperliquid base-tier taker fee, applied on entry and on exit.
DEFAULT_FEE_BPS = 4.5


class ExitReason(Enum):
    STOP = "stop"
    TAKE_PROFIT = "take_profit"
    END_OF_DATA = "end_of_data"


@dataclass(frozen=True)
class BacktestTrade:
    side: Side
    entry_index: int
    exit_index: int
    entry_price: float
    stop_price: float
    take_profit_price: float | None
    exit_price: float
    exit_reason: ExitReason
    r_multiple: float  # net of fees

    @property
    def bars_held(self) -> int:
        return self.exit_index - self.entry_index


@dataclass(frozen=True)
class BacktestResult:
    trades: tuple[BacktestTrade, ...]
    candles_tested: int
    fee_bps: float

    @property
    def count(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for trade in self.trades if trade.r_multiple > 0)

    @property
    def losses(self) -> int:
        return self.count - self.wins

    @property
    def win_rate(self) -> float:
        return self.wins / self.count if self.count else 0.0

    @property
    def total_r(self) -> float:
        return sum(trade.r_multiple for trade in self.trades)

    @property
    def expectancy_r(self) -> float:
        """Average R per trade — the number that decides whether to run this."""
        return self.total_r / self.count if self.count else 0.0

    @property
    def profit_factor(self) -> float:
        gains = sum(t.r_multiple for t in self.trades if t.r_multiple > 0)
        pains = -sum(t.r_multiple for t in self.trades if t.r_multiple < 0)
        if pains == 0:
            return float("inf") if gains > 0 else 0.0
        return gains / pains

    @property
    def max_drawdown_r(self) -> float:
        """Deepest fall from a peak on the cumulative-R curve.

        The number that decides whether the user can actually sit through it.
        """
        peak = running = worst = 0.0
        for trade in self.trades:
            running += trade.r_multiple
            peak = max(peak, running)
            worst = max(worst, peak - running)
        return worst

    def summary(self) -> str:
        if not self.count:
            return f"no trades over {self.candles_tested} candles"
        return (
            f"{self.count:4d} trades  win {self.win_rate:5.1%}  "
            f"expectancy {self.expectancy_r:+.3f}R  total {self.total_r:+7.1f}R  "
            f"PF {self.profit_factor:4.2f}  maxDD {self.max_drawdown_r:5.1f}R"
        )


def _resolve_exit(
    side: Side,
    stop_price: float,
    take_profit_price: float | None,
    candle: Candle,
) -> tuple[float, ExitReason] | None:
    """Did this candle close the position, and at what price?"""
    if side is Side.LONG:
        stop_hit = candle.low <= stop_price
        target_hit = take_profit_price is not None and candle.high >= take_profit_price
    else:
        stop_hit = candle.high >= stop_price
        target_hit = take_profit_price is not None and candle.low <= take_profit_price

    # Both inside one candle: the order is unknowable from OHLC, so assume the loss.
    if stop_hit:
        return stop_price, ExitReason.STOP
    if target_hit:
        return take_profit_price, ExitReason.TAKE_PROFIT
    return None


def run_backtest(
    strategy: Strategy,
    candles: Sequence[Candle],
    *,
    fee_bps: float = DEFAULT_FEE_BPS,
    window: int | None = None,
) -> BacktestResult:
    """Replay `strategy` over `candles`, one position at a time.

    The strategy only ever sees candles up to and including the bar being decided,
    so it cannot read the future.

    `window` caps how many trailing candles each evaluation receives, defaulting to
    the strategy's warmup. That keeps the replay linear instead of quadratic, and —
    more importantly — it feeds the strategy the same bounded buffer the live engine
    will. An EMA over 165 bars differs slightly from one over 20,000, and a backtest
    that used the long version would not be describing the bot that actually runs.
    Pass 0 for unbounded history.
    """
    fee_rate = fee_bps / 10_000.0
    window = strategy.warmup_candles if window is None else window
    trades: list[BacktestTrade] = []

    side: Side | None = None
    entry_index = 0
    entry_price = stop_price = 0.0
    take_profit_price: float | None = None

    def close(exit_index: int, exit_price: float, reason: ExitReason) -> None:
        risk = abs(entry_price - stop_price)
        gross_r = (exit_price - entry_price) * side.sign / risk
        fee_r = fee_rate * (entry_price + exit_price) / risk
        trades.append(
            BacktestTrade(
                side=side,
                entry_index=entry_index,
                exit_index=exit_index,
                entry_price=entry_price,
                stop_price=stop_price,
                take_profit_price=take_profit_price,
                exit_price=exit_price,
                exit_reason=reason,
                r_multiple=gross_r - fee_r,
            )
        )

    start = max(strategy.warmup_candles, 1)
    for index in range(start, len(candles)):
        candle = candles[index]

        if side is not None:
            exit_at = _resolve_exit(side, stop_price, take_profit_price, candle)
            if exit_at is not None:
                close(index, exit_at[0], exit_at[1])
                side = None

        if side is None:
            first = max(0, index + 1 - window) if window > 0 else 0
            signal = strategy.evaluate(candles[first : index + 1])
            if signal is not None:
                side = signal.side
                entry_index = index
                entry_price = signal.entry_price
                stop_price = signal.stop_price
                take_profit_price = signal.take_profit_price

    if side is not None:
        close(len(candles) - 1, candles[-1].close, ExitReason.END_OF_DATA)

    return BacktestResult(
        trades=tuple(trades),
        candles_tested=max(0, len(candles) - start),
        fee_bps=fee_bps,
    )
