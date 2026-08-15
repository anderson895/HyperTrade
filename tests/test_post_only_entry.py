"""Post-only entries: resting for a pullback that may never come.

The difference from an IOC entry is not the fee, it is that **the trade might not
happen**. A backtest built on maker fills has already counted that as a filter, so
an implementation that quietly fills everything would report a different strategy.
"""

import logging

import pytest

from src.broker import PaperBroker
from src.broker.base import BrokerError
from src.config import AppSettings
from src.core.models import FillReason, Side, Timeframe
from src.core.sizing import PositionPlan, plan_position
from src.db import connect
from src.engine import BotEngine
from test_engine import BTC, LONG_SIGNAL, FakeInfo, StubStrategy, make_candles

ENTRY = 63_000.0
STOP = 62_000.0
NOW = 1_700_000_000_000
MINUTE_MS = 60_000


@pytest.fixture
def conn():
    connection = connect(":memory:")
    yield connection
    connection.close()


def make_broker(conn, clock=None):
    return PaperBroker(
        "BTC", BTC, 10_000.0, slippage=0.0, fee_bps=4.5, conn=conn,
        clock=clock or (lambda: NOW),
    )


def make_plan(side=Side.LONG, entry=ENTRY, stop=STOP) -> PositionPlan:
    plan = plan_position(
        side=side, entry_price=entry, stop_price=stop,
        take_profit_price=entry + (entry - stop) * 2 * side.sign,
        risk_usdc=50.0, equity_usdc=10_000.0, leverage=5, asset=BTC,
    )
    assert isinstance(plan, PositionPlan), plan
    return plan


async def rest(broker, plan=None, expire_after=30 * MINUTE_MS):
    plan = plan or make_plan()
    return await broker.open_position(
        plan, plan.entry_price, post_only=True, expire_after_ms=expire_after
    )


# --- resting ---------------------------------------------------------------


async def test_a_post_only_entry_does_not_fill_on_placement(conn):
    """The whole point: it waits. Returning a Fill here would be a lie."""
    broker = make_broker(conn)

    assert await rest(broker) is None
    assert await broker.managed_position() is None
    assert broker.pending_entry() is not None


async def test_it_fills_when_price_comes_back_through_the_limit(conn):
    broker = make_broker(conn)
    await rest(broker)

    fills = await broker.sync(62_999.0)  # a buy limit at 63,000 is now above market

    assert len(fills) == 1
    assert fills[0].reason is FillReason.ENTRY
    assert fills[0].price == pytest.approx(ENTRY)
    assert broker.pending_entry() is None
    assert await broker.managed_position() is not None


async def test_touching_the_limit_is_not_a_fill(conn):
    """Sitting at the touch is the back of the queue. Counting it as filled is the
    easiest way to make a post-only backtest look better than the real thing."""
    broker = make_broker(conn)
    await rest(broker)

    assert await broker.sync(ENTRY) == []
    assert broker.pending_entry() is not None


async def test_a_short_entry_fills_on_a_bounce_upward(conn):
    broker = make_broker(conn)
    await rest(broker, make_plan(side=Side.SHORT, entry=ENTRY, stop=64_000.0))

    assert await broker.sync(62_900.0) == []   # still below: no fill for a sell
    fills = await broker.sync(63_100.0)        # came back up through it

    assert len(fills) == 1
    assert fills[0].side is Side.SHORT


async def test_a_maker_fill_is_not_charged_the_taker_fee(conn):
    """Modelled as zero rather than as a rebate, so paper stays pessimistic."""
    broker = make_broker(conn)
    await rest(broker)

    fill = (await broker.sync(62_999.0))[0]

    assert fill.fee == 0.0


# --- expiring --------------------------------------------------------------


async def test_an_unfilled_entry_is_cancelled_once_it_expires(conn, caplog):
    clock = {"now": NOW}
    broker = make_broker(conn, clock=lambda: clock["now"])
    await rest(broker, expire_after=30 * MINUTE_MS)

    clock["now"] = NOW + 31 * MINUTE_MS
    with caplog.at_level(logging.INFO):
        assert await broker.sync(63_500.0) == []   # never came back

    assert broker.pending_entry() is None
    assert "expired unfilled" in caplog.text


async def test_it_still_fills_on_the_very_poll_it_expires(conn):
    """Price arriving and the clock running out on the same poll is a fill: the
    order was live when the price traded."""
    clock = {"now": NOW}
    broker = make_broker(conn, clock=lambda: clock["now"])
    await rest(broker, expire_after=30 * MINUTE_MS)

    clock["now"] = NOW + 31 * MINUTE_MS
    fills = await broker.sync(62_999.0)

    assert len(fills) == 1


async def test_cancelling_by_hand_reports_whether_there_was_anything_to_cancel(conn):
    broker = make_broker(conn)

    assert await broker.cancel_entry() is False

    await rest(broker)
    assert await broker.cancel_entry() is True
    assert broker.pending_entry() is None


async def test_a_second_entry_while_one_rests_is_refused(conn):
    broker = make_broker(conn)
    await rest(broker)

    with pytest.raises(BrokerError, match="already resting"):
        await rest(broker)


async def test_a_resting_order_does_not_survive_a_restart(conn):
    """The exchange would have cancelled it while the app was shut; reviving it
    from local state would put money on a level nobody is watching."""
    broker = make_broker(conn)
    await rest(broker)

    revived = make_broker(conn)

    assert revived.pending_entry() is None


# --- the engine's side -----------------------------------------------------


def build_engine(conn, broker, **overrides):
    settings = AppSettings(
        timeframe=Timeframe.M15,
        risk_usdc=50.0,
        leverage=5,
        paper_starting_balance=10_000.0,
        post_only_entry=True,
        entry_expiry_candles=2,
    )
    for key, value in overrides.items():
        setattr(settings, key, value)
    return BotEngine(
        settings=settings,
        info=FakeInfo(make_candles(5)),
        broker=broker,
        strategy=StubStrategy(LONG_SIGNAL),
        asset=BTC,
        conn=conn,
        throttle_requests=False,
    )


async def test_the_engine_rests_the_entry_instead_of_crossing(conn):
    broker = make_broker(conn)
    engine = build_engine(conn, broker)
    await engine.prepare()

    await engine._on_candle_close()

    assert broker.pending_entry() is not None
    assert await broker.managed_position() is None


async def test_the_expiry_is_the_configured_number_of_candles(conn):
    broker = make_broker(conn)
    engine = build_engine(conn, broker, entry_expiry_candles=2)
    await engine.prepare()

    await engine._on_candle_close()
    pending = broker.pending_entry()

    window_ms = pending.expire_at_ms - pending.placed_at_ms
    assert window_ms == pytest.approx(2 * 15 * MINUTE_MS, rel=0.01)


async def test_no_second_signal_is_acted_on_while_one_rests(conn):
    """Two orders that both filled would double the size, and the strategy has no
    view on the one already placed."""
    broker = make_broker(conn)
    engine = build_engine(conn, broker)
    await engine.prepare()
    await engine._on_candle_close()

    assert await engine._reason_not_to_trade() == "an entry order is already resting"


async def test_ioc_remains_the_default(conn):
    """Post-only changes which trades happen at all, so it is opt-in."""
    broker = make_broker(conn)
    engine = build_engine(conn, broker, post_only_entry=False)
    await engine.prepare()

    await engine._on_candle_close()

    assert broker.pending_entry() is None
    assert await broker.managed_position() is not None
