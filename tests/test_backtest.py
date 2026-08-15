"""The backtester itself — a bug here would give false confidence in a strategy."""

from collections.abc import Sequence

import pytest

from src.backtest import ExitReason, run_backtest
from src.core.models import Candle, Side, Signal
from src.strategy.base import Strategy


class SignalOnBar(Strategy):
    """Fires one fixed signal on each chosen bar. Not registered — test-only."""

    name = "signal_on_bar"
    display_name = "test double"

    def __init__(self, bars, side: Side, entry: float, stop: float, target: float | None):
        self.bars = {bars} if isinstance(bars, int) else set(bars)
        self.side = side
        self.entry = entry
        self.stop = stop
        self.target = target

    @property
    def warmup_candles(self) -> int:
        return 0

    def evaluate(self, candles: Sequence[Candle]) -> Signal | None:
        if len(candles) - 1 not in self.bars:
            return None
        return Signal(
            side=self.side,
            entry_price=self.entry,
            stop_price=self.stop,
            take_profit_price=self.target,
            reason="test",
        )


def bar(high: float, low: float, close: float = None) -> Candle:
    return Candle(
        open_time_ms=0,
        close_time_ms=1,
        open=low,
        high=high,
        low=low,
        close=close if close is not None else (high + low) / 2,
        volume=1.0,
        trades=1,
    )


FLAT = bar(100, 100, 100)


def test_target_hit_pays_the_reward_multiple():
    candles = [FLAT, FLAT, bar(121, 99, 120)]
    strategy = SignalOnBar(1, Side.LONG, entry=100, stop=90, target=120)

    result = run_backtest(strategy, candles, fee_bps=0)
    assert result.count == 1
    trade = result.trades[0]
    assert trade.exit_reason is ExitReason.TAKE_PROFIT
    assert trade.r_multiple == pytest.approx(2.0)  # (120-100)/(100-90)


def test_stop_hit_costs_exactly_one_r():
    candles = [FLAT, FLAT, bar(101, 89, 92)]
    strategy = SignalOnBar(1, Side.LONG, entry=100, stop=90, target=120)

    trade = run_backtest(strategy, candles, fee_bps=0).trades[0]
    assert trade.exit_reason is ExitReason.STOP
    assert trade.r_multiple == pytest.approx(-1.0)


def test_a_candle_containing_both_levels_is_scored_as_a_loss():
    """OHLC cannot say which came first, so the pessimistic reading is used."""
    candles = [FLAT, FLAT, bar(125, 85, 120)]
    strategy = SignalOnBar(1, Side.LONG, entry=100, stop=90, target=120)

    trade = run_backtest(strategy, candles, fee_bps=0).trades[0]
    assert trade.exit_reason is ExitReason.STOP
    assert trade.r_multiple == pytest.approx(-1.0)


def test_short_trades_are_scored_the_same_way():
    candles = [FLAT, FLAT, bar(101, 79, 80)]
    strategy = SignalOnBar(1, Side.SHORT, entry=100, stop=110, target=80)

    trade = run_backtest(strategy, candles, fee_bps=0).trades[0]
    assert trade.exit_reason is ExitReason.TAKE_PROFIT
    assert trade.r_multiple == pytest.approx(2.0)


def test_an_open_position_is_closed_at_the_last_close():
    candles = [FLAT, FLAT, bar(105, 99, 104)]
    strategy = SignalOnBar(1, Side.LONG, entry=100, stop=90, target=120)

    trade = run_backtest(strategy, candles, fee_bps=0).trades[0]
    assert trade.exit_reason is ExitReason.END_OF_DATA
    assert trade.r_multiple == pytest.approx(0.4)  # (104-100)/10


def test_fees_are_charged_on_both_sides():
    candles = [FLAT, FLAT, bar(121, 99, 120)]
    strategy = SignalOnBar(1, Side.LONG, entry=100, stop=90, target=120)

    free = run_backtest(strategy, candles, fee_bps=0).trades[0]
    charged = run_backtest(strategy, candles, fee_bps=4.5).trades[0]

    assert charged.r_multiple < free.r_multiple
    # 4.5bps = 0.045% on entry (100) and exit (120), over a 10-wide risk = 0.0099R.
    # Small per trade, but it is charged every time — which is why trade frequency
    # is not free and why a chop filter has to beat this bar to be worth adding.
    assert free.r_multiple - charged.r_multiple == pytest.approx(0.00045 * 220 / 10)


def test_no_signal_means_no_trades():
    result = run_backtest(SignalOnBar(999, Side.LONG, 100, 90, 120), [FLAT] * 5)
    assert result.count == 0
    assert result.expectancy_r == 0.0
    assert "no trades" in result.summary()


def test_a_strategy_cannot_see_the_future():
    """Each evaluation must receive candles only up to the bar being decided."""
    seen: list[int] = []

    class Recorder(SignalOnBar):
        def evaluate(self, candles):
            seen.append(len(candles))
            return None

    candles = [FLAT] * 6
    run_backtest(Recorder(0, Side.LONG, 100, 90, 120), candles)
    assert seen == [2, 3, 4, 5, 6]  # never the full 6 before the last bar


def test_evaluation_is_capped_to_a_rolling_window():
    """The strategy must see the same bounded buffer the live engine will give it."""
    sizes: list[int] = []

    class Recorder(SignalOnBar):
        @property
        def warmup_candles(self) -> int:
            return 4

        def evaluate(self, candles):
            sizes.append(len(candles))
            return None

    run_backtest(Recorder(0, Side.LONG, 100, 90, 120), [FLAT] * 20)
    assert max(sizes) == 4


# --- statistics -----------------------------------------------------------

WIN = bar(121, 99, 120)  # reaches the 120 target
LOSS = bar(101, 89, 92)  # reaches the 90 stop


def mixed_run():
    """Entries on bars 1, 3 and 5, each resolved by the bar that follows it.

    Entry happens at a bar's *close*, so the entry bar's own range can no longer
    touch the stop or the target — resolution starts on the next bar.
    """
    candles = [FLAT, FLAT, WIN, FLAT, LOSS, FLAT, WIN]
    strategy = SignalOnBar({1, 3, 5}, Side.LONG, entry=100, stop=90, target=120)
    return run_backtest(strategy, candles, fee_bps=0)


def test_statistics_over_a_mixed_run():
    result = mixed_run()

    assert [round(trade.r_multiple, 6) for trade in result.trades] == [2.0, -1.0, 2.0]
    assert result.count == 3
    assert result.wins == 2
    assert result.win_rate == pytest.approx(2 / 3)
    assert result.total_r == pytest.approx(3.0)
    assert result.expectancy_r == pytest.approx(1.0)
    assert result.profit_factor == pytest.approx(4.0)  # 4R won / 1R lost


def test_max_drawdown_tracks_the_worst_fall_from_a_peak():
    # Cumulative R runs 2.0 -> 1.0 -> 3.0, so the deepest fall from a peak is 1R.
    assert mixed_run().max_drawdown_r == pytest.approx(1.0)


def test_a_position_open_at_the_end_is_still_reported():
    """Dropping it would flatter the results by hiding an unresolved trade."""
    candles = [FLAT, FLAT, bar(105, 99, 104)]
    result = run_backtest(
        SignalOnBar(1, Side.LONG, entry=100, stop=90, target=120), candles, fee_bps=0
    )
    assert result.count == 1
    assert result.trades[0].exit_reason is ExitReason.END_OF_DATA
