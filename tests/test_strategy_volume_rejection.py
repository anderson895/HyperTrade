"""Volume rejection, checked against the specification it was built from.

Every number here comes from the user's own backtested spec, so these tests are
what stops the implementation quietly drifting away from what was measured.
"""

import pytest

from src.core.models import Candle, Side
from src.strategy import VolumeRejection, available, create

MINUTE_MS = 60_000


def candle(open_, high, low, close, volume=100.0, index=0) -> Candle:
    return Candle(
        open_time_ms=index * 15 * MINUTE_MS,
        close_time_ms=(index + 1) * 15 * MINUTE_MS - 1,
        open=open_, high=high, low=low, close=close,
        volume=volume, trades=10,
    )


def quiet_range(count=97, low=59_000.0, high=61_000.0, volume=100.0):
    """Bars that establish a range without ever breaking it."""
    return [
        candle(60_000, high - 100, low + 100, 60_000, volume, index)
        for index in range(count)
    ]


def with_signal(bar: Candle, history=None) -> list[Candle]:
    history = history or quiet_range()
    return [*history, bar]


# --- the shape of a signal -------------------------------------------------


def test_a_rejected_break_above_the_range_is_a_short():
    """High volume, price pokes above the range, long upper wick, closes inside."""
    strategy = VolumeRejection()
    # Range high is 60,900. This bar reaches 61,500 and closes back at 60,500.
    bar = candle(60_600, 61_500, 60_400, 60_500, volume=300.0, index=97)

    signal = strategy.evaluate(with_signal(bar))

    assert signal is not None
    assert signal.side is Side.SHORT
    assert signal.entry_price == 60_500
    # Stop sits 0.1% beyond the wick that did the rejecting.
    assert signal.stop_price == pytest.approx(61_500 * 1.001)


def test_a_rejected_break_below_the_range_is_a_long():
    strategy = VolumeRejection()
    bar = candle(59_400, 59_600, 58_500, 59_500, volume=300.0, index=97)

    signal = strategy.evaluate(with_signal(bar))

    assert signal is not None
    assert signal.side is Side.LONG
    assert signal.stop_price == pytest.approx(58_500 * 0.999)


def test_the_target_is_two_r_from_the_stop():
    """The user's worked example: entry 60,000, stop 59,000, target 62,000.

    Stop distance 1,000, target distance 2,000 - a 1:2 risk to reward."""
    strategy = VolumeRejection(stop_buffer=0.0, take_profit_rr=2.0)
    history = quiet_range(low=59_500.0, high=60_500.0)
    # Closes at 60,000 having been rejected from a low of 59,000.
    bar = candle(59_900, 60_050, 59_000, 60_000, volume=300.0, index=97)

    signal = strategy.evaluate(with_signal(bar, history))

    assert signal is not None
    assert signal.entry_price == 60_000
    assert signal.stop_price == pytest.approx(59_000)
    assert signal.take_profit_price == pytest.approx(62_000)


def test_the_reward_multiple_is_configurable():
    strategy = VolumeRejection(stop_buffer=0.0, take_profit_rr=3.0)
    history = quiet_range(low=59_500.0, high=60_500.0)
    bar = candle(59_900, 60_050, 59_000, 60_000, volume=300.0, index=97)

    signal = strategy.evaluate(with_signal(bar, history))

    assert signal.take_profit_price == pytest.approx(63_000)


# --- what disqualifies a bar ----------------------------------------------


def test_no_signal_without_a_volume_spike():
    """The volume is the whole premise: someone was there in size and lost."""
    strategy = VolumeRejection()
    bar = candle(60_600, 61_500, 60_400, 60_500, volume=150.0, index=97)  # 1.5x

    assert strategy.evaluate(with_signal(bar)) is None


def test_no_signal_when_the_wick_is_too_short():
    """Closing at the high is a breakout that held, not one that was rejected."""
    strategy = VolumeRejection()
    bar = candle(60_500, 61_500, 60_400, 61_450, volume=300.0, index=97)

    assert strategy.evaluate(with_signal(bar)) is None


def test_no_signal_when_the_close_stays_outside_the_range():
    """Price broke out and stayed out. That is a breakout; this system fades
    failures, not successes."""
    strategy = VolumeRejection()
    # Long upper wick, but the close is above the 60,900 range high.
    bar = candle(60_950, 61_800, 60_920, 61_000, volume=300.0, index=97)

    assert strategy.evaluate(with_signal(bar)) is None


def test_no_signal_when_the_range_was_never_broken():
    strategy = VolumeRejection()
    bar = candle(60_500, 60_800, 60_200, 60_300, volume=300.0, index=97)

    assert strategy.evaluate(with_signal(bar)) is None


def test_a_bar_with_no_range_is_ignored():
    """A flat bar has no wick to measure, and dividing by its length would raise."""
    strategy = VolumeRejection()
    bar = candle(60_000, 60_000, 60_000, 60_000, volume=300.0, index=97)

    assert strategy.evaluate(with_signal(bar)) is None


# --- the window ------------------------------------------------------------


def test_the_signal_bar_is_not_part_of_its_own_range():
    """Regression: included in the range, a new high is always its own high, and no
    break could ever be detected."""
    strategy = VolumeRejection()
    bar = candle(60_600, 61_500, 60_400, 60_500, volume=300.0, index=97)

    signal = strategy.evaluate(with_signal(bar))

    assert signal is not None  # would be None if the bar set its own range high


def test_nothing_is_decided_before_the_window_is_full():
    strategy = VolumeRejection()
    short = quiet_range(count=50)
    bar = candle(60_600, 61_500, 60_400, 60_500, volume=300.0, index=50)

    assert strategy.evaluate([*short, bar]) is None


def test_warmup_covers_the_longer_of_the_two_lookbacks():
    assert VolumeRejection(range_lookback=96, volume_lookback=20).warmup_candles == 97
    assert VolumeRejection(range_lookback=10, volume_lookback=50).warmup_candles == 51


# --- configuration ---------------------------------------------------------


@pytest.mark.parametrize("kwargs", [
    {"range_lookback": 1},
    {"volume_lookback": 1},
    {"volume_multiple": 0},
    {"wick_fraction": 0},
    {"wick_fraction": 1.5},
    {"stop_buffer": -0.1},
    {"take_profit_rr": 0},
])
def test_impossible_settings_are_refused(kwargs):
    with pytest.raises(ValueError):
        VolumeRejection(**kwargs)


def test_the_registry_offers_it_for_the_settings_dropdown():
    assert "volume_rejection" in available()
    assert isinstance(create("volume_rejection"), VolumeRejection)


def test_the_parameters_are_reported_for_the_startup_log():
    parameters = VolumeRejection().parameters()
    assert parameters["range_lookback"] == 96
    assert parameters["volume_multiple"] == 2.0
    assert parameters["take_profit_rr"] == 2.0


def test_it_can_estimate_a_stop_distance_for_the_sizing_check():
    """Without this the pre-flight check cannot tell settings that can never fit
    from a market that simply is not signalling."""
    strategy = VolumeRejection()
    distance = strategy.typical_stop_distance(quiet_range())

    assert distance is not None and distance > 0
