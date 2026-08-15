"""Trend-following signal behaviour on synthetic series."""

import pytest

from src.core.models import AssetMeta, Candle, Side
from src.core.sizing import PositionPlan, plan_position
from src.strategy import TrendFollowing, available, create

BTC = AssetMeta(name="BTC", asset_index=0, sz_decimals=5, max_leverage=40)


def candles_from_closes(closes, spread: float = 1.0) -> list[Candle]:
    """Build candles whose highs/lows straddle each close by `spread`."""
    candles = []
    previous = closes[0]
    for index, close in enumerate(closes):
        candles.append(
            Candle(
                open_time_ms=index * 60_000,
                close_time_ms=(index + 1) * 60_000 - 1,
                open=previous,
                high=max(previous, close) + spread,
                low=min(previous, close) - spread,
                close=close,
                volume=1.0,
                trades=1,
            )
        )
        previous = close
    return candles


def collect_signals(strategy, candles):
    """Every signal the strategy would have produced, bar by bar."""
    signals = []
    for index in range(len(candles)):
        signal = strategy.evaluate(candles[: index + 1])
        if signal is not None:
            signals.append((index, signal))
    return signals


def v_shaped_closes(length: int = 400, bottom: float = 50.0, top: float = 150.0):
    """Down then up — guarantees a bullish crossover in the second half."""
    half = length // 2
    down = [top - (top - bottom) * i / half for i in range(half)]
    up = [bottom + (top - bottom) * i / (length - half) for i in range(length - half)]
    return down + up


def inverted_v_closes(length: int = 400, bottom: float = 50.0, top: float = 150.0):
    """Up then down — guarantees a bearish crossover in the second half.

    Note this is NOT `reversed(v_shaped_closes())`: reversing a V gives back a V.
    """
    half = length // 2
    up = [bottom + (top - bottom) * i / half for i in range(half)]
    down = [top - (top - bottom) * i / (length - half) for i in range(length - half)]
    return up + down


# --- signals --------------------------------------------------------------


def test_uptrend_produces_a_long_and_no_short():
    signals = collect_signals(TrendFollowing(), candles_from_closes(v_shaped_closes()))
    sides = {signal.side for _, signal in signals}
    assert Side.LONG in sides
    assert Side.SHORT not in sides


def test_downtrend_produces_a_short_and_no_long():
    signals = collect_signals(TrendFollowing(), candles_from_closes(inverted_v_closes()))
    sides = {signal.side for _, signal in signals}
    assert Side.SHORT in sides
    assert Side.LONG not in sides


def test_a_flat_market_produces_nothing():
    """No crossover, no trade. Days with no trades are normal."""
    assert collect_signals(TrendFollowing(), candles_from_closes([100.0] * 400)) == []


def test_nothing_fires_during_warmup():
    candles = candles_from_closes(v_shaped_closes(length=40))
    assert TrendFollowing().evaluate(candles) is None


def test_signal_explains_itself_for_the_log():
    _, signal = collect_signals(TrendFollowing(), candles_from_closes(v_shaped_closes()))[0]
    assert "EMA21 crossed above EMA55" in signal.reason
    assert "ATR(14)" in signal.reason


# --- exit geometry --------------------------------------------------------


def test_long_stop_is_below_entry_and_target_is_two_r_above():
    _, signal = collect_signals(TrendFollowing(), candles_from_closes(v_shaped_closes()))[0]
    assert signal.stop_price < signal.entry_price < signal.take_profit_price
    risk = signal.entry_price - signal.stop_price
    reward = signal.take_profit_price - signal.entry_price
    assert reward == pytest.approx(2.0 * risk)


def test_short_stop_is_above_entry_and_target_is_two_r_below():
    _, signal = collect_signals(TrendFollowing(), candles_from_closes(inverted_v_closes()))[0]
    assert signal.take_profit_price < signal.entry_price < signal.stop_price
    risk = signal.stop_price - signal.entry_price
    reward = signal.entry_price - signal.take_profit_price
    assert reward == pytest.approx(2.0 * risk)


def test_stop_distance_scales_with_volatility():
    """A wider market gets a wider stop — that is the point of an ATR stop."""
    closes = v_shaped_closes()
    _, calm = collect_signals(TrendFollowing(), candles_from_closes(closes, spread=1.0))[0]
    _, wild = collect_signals(TrendFollowing(), candles_from_closes(closes, spread=10.0))[0]
    assert (wild.entry_price - wild.stop_price) > (calm.entry_price - calm.stop_price)


def test_reward_risk_is_configurable():
    strategy = TrendFollowing(reward_risk=3.0)
    _, signal = collect_signals(strategy, candles_from_closes(v_shaped_closes()))[0]
    risk = signal.entry_price - signal.stop_price
    assert signal.take_profit_price - signal.entry_price == pytest.approx(3.0 * risk)


# --- the optional chop filters --------------------------------------------


def test_both_filters_are_off_by_default():
    """Neither filter is enabled without backtest evidence that it earns its place.

    Measured on synthetic series, a short slope lookback removed ~13% of trend
    entries to remove ~1% of chop entries — strictly worse than no filter. See
    tools/run_backtest.py for the real-candle comparison.
    """
    strategy = TrendFollowing()
    assert strategy.slope_lookback == 0
    assert strategy.trend_filter_period == 0


def test_slope_filter_vetoes_an_entry_the_slow_ema_disagrees_with():
    """A long lookback blocks the entry at a trend reversal — its cost, not a bug."""
    candles = candles_from_closes(v_shaped_closes())
    unfiltered = collect_signals(TrendFollowing(), candles)
    filtered = collect_signals(TrendFollowing(slope_lookback=40), candles)

    assert len(unfiltered) > 0
    assert len(filtered) < len(unfiltered)


def test_trend_filter_vetoes_longs_below_its_ema():
    """The crossover off a V-bottom fires while price is still under the EMA200."""
    candles = candles_from_closes(v_shaped_closes(length=1600))
    unfiltered = collect_signals(TrendFollowing(), candles)
    filtered = collect_signals(TrendFollowing(trend_filter_period=200), candles)

    assert len(unfiltered) > 0
    assert len(filtered) < len(unfiltered)


def test_warmup_grows_when_a_filter_needs_more_history():
    assert TrendFollowing(trend_filter_period=200).warmup_candles > TrendFollowing().warmup_candles


# --- composition with sizing ----------------------------------------------


def test_a_signal_can_be_sized_into_a_valid_plan():
    """The strategy hands sizing something it can actually work with."""
    _, signal = collect_signals(TrendFollowing(), candles_from_closes(v_shaped_closes()))[0]
    plan = plan_position(
        side=signal.side,
        entry_price=signal.entry_price,
        stop_price=signal.stop_price,
        take_profit_price=signal.take_profit_price,
        risk_usdc=5.0,
        equity_usdc=1_000.0,
        leverage=2,
        asset=BTC,
    )
    assert isinstance(plan, PositionPlan), plan
    assert plan.risk_usdc == pytest.approx(5.0, rel=1e-3)


# --- registry -------------------------------------------------------------


def test_registry_exposes_the_strategy_for_the_settings_dropdown():
    assert "trend_following" in available()
    assert isinstance(create("trend_following", fast_period=9, slow_period=21), TrendFollowing)


def test_unknown_strategy_is_reported_clearly():
    with pytest.raises(KeyError, match="unknown strategy"):
        create("nope")


@pytest.mark.parametrize(
    "params",
    [
        {"fast_period": 55, "slow_period": 21},  # fast must be shorter
        {"fast_period": 21, "slow_period": 21},
        {"atr_stop_multiple": 0},
        {"reward_risk": -1},
        {"atr_period": 0},
    ],
)
def test_nonsense_parameters_are_rejected(params):
    with pytest.raises(ValueError):
        TrendFollowing(**params)
