"""The trailing stop: following a winning trade up, and never letting it back down.

The dangerous failure here is not a stop that trails too little — it is one that
moves the wrong way, which hands back protection the trade has already earned.
Most of these tests exist to pin that down.
"""

import logging

import pytest

from src.broker import PaperBroker
from src.config import AppSettings
from src.core.models import Side, Timeframe
from src.core.sizing import PositionPlan, plan_position
from src.db import connect
from src.engine import BotEngine
from test_engine import BTC, MARK, FakeInfo, StubStrategy, make_candles

ENTRY = 63_000.0
STOP = 62_000.0          # 1,000 of risk
RISK_DISTANCE = ENTRY - STOP


@pytest.fixture
def conn():
    connection = connect(":memory:")
    yield connection
    connection.close()


def build(conn, **overrides):
    settings = AppSettings(
        timeframe=Timeframe.H1,
        risk_usdc=50.0,
        leverage=5,
        paper_starting_balance=10_000.0,
        trailing_enabled=True,
        trailing_activation_rr=0.0,
        trailing_distance_pct=0.004,
    )
    for key, value in overrides.items():
        setattr(settings, key, value)

    broker = PaperBroker("BTC", BTC, 10_000.0, slippage=0.0, fee_bps=0.0, conn=conn)
    engine = BotEngine(
        settings=settings,
        info=FakeInfo(make_candles(5)),
        broker=broker,
        strategy=StubStrategy(None),
        asset=BTC,
        conn=conn,
        throttle_requests=False,
    )
    return engine, broker


def make_plan(side=Side.LONG, entry=ENTRY, stop=STOP, target=None) -> PositionPlan:
    plan = plan_position(
        side=side, entry_price=entry, stop_price=stop,
        take_profit_price=target or (entry + (entry - stop) * 2 * side.sign),
        risk_usdc=50.0, equity_usdc=10_000.0, leverage=5, asset=BTC,
    )
    assert isinstance(plan, PositionPlan), plan
    return plan


async def open_long(broker, **kwargs):
    plan = make_plan(**kwargs)
    await broker.open_position(plan, plan.entry_price)
    return plan


async def stop_of(broker) -> float:
    held = await broker.managed_position()
    return held.stop_price


# --- following a winner ----------------------------------------------------


async def test_the_stop_follows_a_long_up(conn):
    engine, broker = build(conn)
    await open_long(broker)

    await engine._trail_stop(63_500.0)

    # 0.4% behind the best price seen.
    assert await stop_of(broker) == pytest.approx(63_500.0 * 0.996)


async def test_the_stop_follows_a_short_down(conn):
    engine, broker = build(conn)
    await open_long(broker, side=Side.SHORT, entry=ENTRY, stop=64_000.0)

    await engine._trail_stop(62_500.0)

    assert await stop_of(broker) == pytest.approx(62_500.0 * 1.004)


async def test_the_stop_tracks_the_best_price_not_the_latest(conn):
    """Price ran to 64,000 and came back. The stop keeps what the run earned."""
    engine, broker = build(conn)
    await open_long(broker)

    await engine._trail_stop(64_000.0)
    high_water = await stop_of(broker)
    await engine._trail_stop(63_200.0)

    assert await stop_of(broker) == pytest.approx(high_water)


async def test_the_stop_never_moves_further_away(conn):
    """The one failure that matters: a stop that loosens gives back protection."""
    engine, broker = build(conn)
    await open_long(broker)
    await engine._trail_stop(64_000.0)
    tightened = await stop_of(broker)

    for mark in (63_800.0, 63_100.0, 63_000.0, 62_500.0):
        await engine._trail_stop(mark)
        assert await stop_of(broker) == pytest.approx(tightened)


# --- when it should do nothing ---------------------------------------------


async def test_nothing_moves_while_the_trade_is_not_yet_in_profit(conn):
    """A trailing stop below entry locks in nothing; it is just a tighter loss."""
    engine, broker = build(conn)
    await open_long(broker)

    await engine._trail_stop(63_050.0)  # 0.4% back from here is below entry

    assert await stop_of(broker) == pytest.approx(STOP)


async def test_nothing_moves_before_the_activation_threshold(conn):
    engine, broker = build(conn, trailing_activation_rr=1.0)
    await open_long(broker)

    await engine._trail_stop(63_500.0)  # only 0.5R of profit
    assert await stop_of(broker) == pytest.approx(STOP)

    await engine._trail_stop(64_100.0)  # past 1R
    assert await stop_of(broker) > STOP


async def test_nothing_moves_when_trailing_is_switched_off(conn):
    engine, broker = build(conn, trailing_enabled=False)
    await open_long(broker)

    await engine._trail_stop(65_000.0)

    assert await stop_of(broker) == pytest.approx(STOP)


async def test_nothing_happens_when_flat(conn):
    engine, _ = build(conn)
    await engine._trail_stop(63_500.0)  # must not raise


async def test_the_peak_does_not_carry_into_the_next_trade(conn):
    """Regression: a high-water mark left over from a closed trade would put the
    next trade's stop wherever the last one happened to peak."""
    engine, broker = build(conn)
    await open_long(broker)
    await engine._trail_stop(66_000.0)
    await broker.close_position(66_000.0)

    await engine._trail_stop(66_000.0)  # flat: clears the peak
    await open_long(broker)
    await engine._trail_stop(63_100.0)

    assert await stop_of(broker) == pytest.approx(STOP)


# --- when the venue refuses ------------------------------------------------


async def test_a_refused_move_is_logged_and_survived(conn, caplog):
    """The old stop is still with the venue, so the position is protected at the
    old level - worth saying, not worth stopping for."""
    from src.broker.base import BrokerError

    engine, broker = build(conn)
    await open_long(broker)

    async def refuse(new_stop):
        raise BrokerError("venue said no")

    broker.move_stop = refuse

    with caplog.at_level(logging.WARNING):
        await engine._trail_stop(64_000.0)

    assert "could not move the trailing stop" in caplog.text


# --- the paper broker's half ------------------------------------------------


async def test_moving_the_paper_stop_changes_what_settles(conn):
    broker = PaperBroker("BTC", BTC, 10_000.0, slippage=0.0, fee_bps=0.0, conn=conn)
    await broker.open_position(make_plan(), ENTRY)

    assert await broker.move_stop(63_200.0)

    fills = await broker.sync(63_150.0)  # under the new stop, above the old one
    assert len(fills) == 1
    assert fills[0].reason.value == "stop_loss"


async def test_moving_the_stop_while_flat_reports_nothing_to_move(conn):
    broker = PaperBroker("BTC", BTC, 10_000.0, slippage=0.0, fee_bps=0.0, conn=conn)

    assert await broker.move_stop(63_200.0) is False


async def test_a_moved_stop_survives_a_restart(conn):
    """It is persisted like the rest of the paper state, or a restart would hand
    back every gain the trail had locked in."""
    broker = PaperBroker("BTC", BTC, 10_000.0, slippage=0.0, fee_bps=0.0, conn=conn)
    await broker.open_position(make_plan(), ENTRY)
    await broker.move_stop(63_200.0)

    revived = PaperBroker("BTC", BTC, 10_000.0, slippage=0.0, fee_bps=0.0, conn=conn)
    held = await revived.managed_position()

    assert held.stop_price == pytest.approx(63_200.0)
