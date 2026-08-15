"""The bot loop, driven by a fake price feed against the real paper broker."""

import time
from collections.abc import Sequence

import pytest

from src.broker import Fill, FillReason, PaperBroker
from src.config import AppSettings
from src.core.models import AssetMeta, Candle, Side, Signal, Timeframe, TradingMode
from src.db import connect
from src.engine import BotEngine
from src.store import record_fill
from src.strategy.base import Strategy

BTC = AssetMeta(name="BTC", asset_index=0, sz_decimals=5, max_leverage=40)
MARK = 63_000.0
HOUR_MS = 3_600_000


def make_candles(count: int, price: float = MARK) -> list[Candle]:
    return [
        Candle(
            open_time_ms=index * HOUR_MS,
            close_time_ms=(index + 1) * HOUR_MS - 1,
            open=price,
            high=price + 10,
            low=price - 10,
            close=price,
            volume=1.0,
            trades=1,
        )
        for index in range(count)
    ]


class FakeInfo:
    """Stands in for HyperliquidInfo. Append to `candles` to close a new one."""

    def __init__(self, candles: list[Candle], mark: float = MARK):
        self.candles = candles
        self.mark = mark
        self.mid_calls = 0
        self.candle_calls = 0

    async def mid_price(self, coin: str) -> float:
        self.mid_calls += 1
        return self.mark

    async def recent_candles(self, coin: str, timeframe: Timeframe, count: int):
        self.candle_calls += 1
        return self.candles[-count:]


class StubStrategy(Strategy):
    """Returns whatever signal it is told to. Not registered — test-only."""

    name = "stub"
    display_name = "test double"

    def __init__(self, signal: Signal | None = None):
        self.signal = signal
        self.calls = 0

    @property
    def warmup_candles(self) -> int:
        return 3

    def evaluate(self, candles: Sequence[Candle]) -> Signal | None:
        self.calls += 1
        return self.signal


LONG_SIGNAL = Signal(
    side=Side.LONG,
    entry_price=MARK,
    stop_price=62_000.0,
    take_profit_price=65_000.0,
    reason="test signal",
)


@pytest.fixture
def conn():
    connection = connect(":memory:")
    yield connection
    connection.close()


def build(conn, *, signal=None, throttle=False, **overrides):
    settings = AppSettings(
        timeframe=Timeframe.H1,
        risk_usdc=50.0,
        leverage=5,
        paper_starting_balance=1_000.0,
    )
    for key, value in overrides.items():
        setattr(settings, key, value)

    info = FakeInfo(make_candles(5))
    broker = PaperBroker("BTC", BTC, 1_000.0, slippage=0.0, fee_bps=0.0, conn=conn)
    engine = BotEngine(
        settings=settings,
        info=info,
        broker=broker,
        strategy=StubStrategy(signal),
        asset=BTC,
        conn=conn,
        throttle_requests=throttle,
    )
    return engine, info, broker


# --- warmup ---------------------------------------------------------------


async def test_prepare_loads_closed_candles_only(conn):
    """The candle still forming must never reach the strategy."""
    engine, info, _ = build(conn)
    await engine.prepare()

    assert len(engine.candles) == 3
    assert engine.candles[-1].open_time_ms < info.candles[-1].open_time_ms


async def test_prepare_refuses_when_history_is_too_short(conn):
    engine, info, _ = build(conn)
    info.candles = make_candles(2)
    with pytest.raises(RuntimeError, match="needs 3"):
        await engine.prepare()


class AtrStrategy(StubStrategy):
    """A strategy that reports a stop distance, like the real one does."""

    def __init__(self, distance: float, signal=None):
        super().__init__(signal)
        self.distance = distance

    def typical_stop_distance(self, candles):
        return self.distance


async def test_settings_that_can_never_trade_are_flagged_at_start(conn):
    """Found the hard way: 5 USDC of risk at 2x on a 99 USDC account needs 373 USDC
    of notional and gets 198. Every entry is rejected, and a bot that rejects
    everything looks exactly like a market that never signals."""
    engine, _, broker = build(conn, risk_usdc=5.0, leverage=2)
    engine.strategy = AtrStrategy(distance=845.0)
    broker.reset(99.72)
    await engine.load_history()

    warning = await engine.check_settings_can_trade()

    assert warning is not None
    assert "exceeds_leverage_cap" in warning
    assert "raise leverage to 4x" in warning
    assert "lower the risk to about 2.50 USDC" in warning


async def test_settings_that_fit_produce_no_warning(conn):
    engine, _, broker = build(conn, risk_usdc=2.0, leverage=5)
    engine.strategy = AtrStrategy(distance=845.0)
    broker.reset(99.72)
    await engine.load_history()

    assert await engine.check_settings_can_trade() is None


async def test_the_check_is_skipped_when_the_strategy_will_not_say(conn):
    """A strategy that cannot report a stop distance is not guessed at."""
    engine, _, _ = build(conn)
    await engine.load_history()
    assert await engine.check_settings_can_trade() is None


async def test_history_loads_without_starting_the_bot(conn):
    """The chart must have candles from the moment the window opens."""
    engine, _, _ = build(conn)
    await engine.load_history()

    assert len(engine.candles) == 3
    assert not engine.is_running


# --- polling while stopped ------------------------------------------------


async def test_polling_settles_a_stop_while_the_bot_is_stopped(conn):
    """STOP promises the stop still stands. In paper mode, polling is what honours it.

    Without this the position would sit unprotected the moment the user pressed
    STOP, and the promise in the log would be a lie.
    """
    engine, info, broker = build(conn, signal=LONG_SIGNAL)
    await engine.prepare()
    info.candles.extend(make_candles(6)[-1:])
    await engine.tick()
    assert await broker.managed_position() is not None

    await engine.stop()
    assert not engine.is_running

    info.mark = 62_000.0  # the stop
    await engine.poll()

    assert await broker.managed_position() is None
    assert broker.balance == pytest.approx(950.0)


async def test_polling_while_stopped_never_opens_a_trade(conn):
    engine, info, broker = build(conn, signal=LONG_SIGNAL)
    await engine.load_history()

    info.candles.extend(make_candles(6)[-1:])
    await engine.poll()

    assert await broker.managed_position() is None
    assert engine.strategy.calls == 0


async def test_poll_reports_a_closed_candle(conn):
    engine, info, _ = build(conn)
    await engine.load_history()

    assert await engine.poll() is False
    info.candles.extend(make_candles(6)[-1:])
    assert await engine.poll() is True


async def test_repeated_polls_do_not_hammer_the_endpoint(conn):
    """The UI polls once a second; the engine must not turn that into 60 requests."""
    engine, info, _ = build(conn, throttle=True)
    await engine.load_history()
    info.mid_calls = 0
    info.candle_calls = 0

    for _ in range(20):
        await engine.poll()

    assert info.mid_calls == 1  # rate-limited to one a second
    assert info.candle_calls <= 1


# --- entries --------------------------------------------------------------


async def test_no_new_candle_means_no_decision(conn):
    engine, _, broker = build(conn, signal=LONG_SIGNAL)
    await engine.prepare()
    await engine.tick()

    assert engine.strategy.calls == 0
    assert await broker.managed_position() is None


async def test_a_closed_candle_with_a_signal_opens_a_position(conn):
    engine, info, broker = build(conn, signal=LONG_SIGNAL)
    await engine.prepare()

    info.candles.extend(make_candles(6)[-1:])  # one more candle closes
    await engine.tick()

    held = await broker.managed_position()
    assert held is not None
    assert held.position.side is Side.LONG
    assert held.position.abs_size == pytest.approx(0.05)  # 50 USDC / 1000 stop distance
    assert held.stop_price == pytest.approx(62_000.0)


async def test_no_signal_means_no_position(conn):
    engine, info, broker = build(conn, signal=None)
    await engine.prepare()
    info.candles.extend(make_candles(6)[-1:])
    await engine.tick()

    assert engine.strategy.calls == 1
    assert await broker.managed_position() is None


async def test_a_second_signal_is_ignored_while_holding(conn):
    """One position at a time, so the strategy is not even consulted."""
    engine, info, broker = build(conn, signal=LONG_SIGNAL)
    await engine.prepare()

    info.candles.extend(make_candles(6)[-1:])
    await engine.tick()
    calls_after_entry = engine.strategy.calls

    info.candles.extend(make_candles(7)[-1:])
    await engine.tick()

    assert engine.strategy.calls == calls_after_entry
    assert (await broker.account_state()).positions[0].abs_size == pytest.approx(0.05)


async def test_the_news_blackout_blocks_entries(conn):
    engine, info, broker = build(conn, signal=LONG_SIGNAL, economic_data_day_block=True)
    await engine.prepare()
    info.candles.extend(make_candles(6)[-1:])
    await engine.tick()

    assert await broker.managed_position() is None


async def test_a_trade_that_does_not_fit_is_rejected_not_resized(conn):
    """Leverage is a cap. A trade over it is refused, never quietly shrunk."""
    engine, info, broker = build(conn, signal=LONG_SIGNAL, risk_usdc=50.0, leverage=1)
    broker.reset(100.0)  # 0.05 BTC of notional needs far more than 100 USDC at 1x
    await engine.prepare()
    info.candles.extend(make_candles(6)[-1:])
    await engine.tick()

    assert await broker.managed_position() is None
    assert broker.balance == pytest.approx(100.0)  # untouched


# --- exits ----------------------------------------------------------------


async def test_a_triggered_stop_is_settled_on_the_next_tick(conn):
    engine, info, broker = build(conn, signal=LONG_SIGNAL)
    await engine.prepare()
    info.candles.extend(make_candles(6)[-1:])
    await engine.tick()

    info.mark = 62_000.0
    await engine.tick()

    assert await broker.managed_position() is None
    assert broker.balance == pytest.approx(950.0)  # the 50 USDC that was at risk


# --- stopping -------------------------------------------------------------


async def test_stopping_the_bot_leaves_the_position_alone(conn):
    """STOP means stop trading, not market-close at whatever the book offers."""
    engine, info, broker = build(conn, signal=LONG_SIGNAL)
    await engine.start()
    info.candles.extend(make_candles(6)[-1:])
    await engine.tick()

    await engine.stop()

    assert not engine.is_running
    held = await broker.managed_position()
    assert held is not None
    assert held.stop_price == pytest.approx(62_000.0)


async def test_close_now_is_the_explicit_way_out(conn):
    engine, info, broker = build(conn, signal=LONG_SIGNAL)
    await engine.prepare()
    info.candles.extend(make_candles(6)[-1:])
    await engine.tick()

    fill = await engine.close_now()

    assert fill is not None
    assert fill.reason is FillReason.MANUAL_CLOSE
    assert await broker.managed_position() is None


# --- daily loss limit -----------------------------------------------------


async def test_the_daily_loss_limit_halts_new_entries(conn):
    engine, info, broker = build(conn, signal=LONG_SIGNAL, daily_loss_limit_usdc=40.0)
    record_fill(
        conn,
        TradingMode.PAPER,
        Fill(
            time_ms=int(time.time() * 1000),
            coin="BTC",
            side=Side.LONG,
            size=0.05,
            price=62_000.0,
            fee=0.0,
            reason=FillReason.STOP_LOSS,
            realised_pnl=-50.0,  # already past the 40 USDC limit
        ),
    )

    await engine.prepare()
    info.candles.extend(make_candles(6)[-1:])
    await engine.tick()

    assert await broker.managed_position() is None


async def test_trading_continues_while_under_the_limit(conn):
    engine, info, broker = build(conn, signal=LONG_SIGNAL, daily_loss_limit_usdc=500.0)
    await engine.prepare()
    info.candles.extend(make_candles(6)[-1:])
    await engine.tick()

    assert await broker.managed_position() is not None
