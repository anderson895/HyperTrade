"""The paper account. If these lie, every paper result is worthless."""

import pytest

from src.broker import FillReason, PaperBroker
from src.broker.base import BrokerError
from src.core.models import AssetMeta, MarginMode, Side, TradingMode
from src.core.sizing import PositionPlan, plan_position
from src.db import connect
from src.store import list_fills, statistics

BTC = AssetMeta(name="BTC", asset_index=0, sz_decimals=5, max_leverage=40)

ENTRY = 63_000.0
STOP = 62_000.0  # 1,000 away
TARGET = 65_000.0  # 2R
RISK = 50.0
START = 1_000.0


@pytest.fixture
def conn():
    connection = connect(":memory:")
    yield connection
    connection.close()


def make_broker(conn=None, *, slippage=0.0, fee_bps=0.0, balance=START, clock=None):
    return PaperBroker(
        "BTC",
        BTC,
        balance,
        slippage=slippage,
        fee_bps=fee_bps,
        conn=conn,
        clock=clock or (lambda: 1_700_000_000_000),
    )


def make_plan(side=Side.LONG, entry=ENTRY, stop=STOP, target=TARGET, equity=START) -> PositionPlan:
    plan = plan_position(
        side=side,
        entry_price=entry,
        stop_price=stop,
        take_profit_price=target,
        risk_usdc=RISK,
        equity_usdc=equity,
        leverage=5,
        asset=BTC,
    )
    assert isinstance(plan, PositionPlan), plan
    return plan


# --- the promise the risk setting makes -----------------------------------


async def test_a_stop_out_costs_the_planned_risk():
    """Risk per trade said 50 USDC. Losing must cost 50 USDC."""
    broker = make_broker()
    plan = make_plan()
    assert plan.size == 0.05

    await broker.open_position(plan, ENTRY)
    fills = await broker.sync(STOP)

    assert len(fills) == 1
    assert fills[0].reason is FillReason.STOP_LOSS
    assert fills[0].realised_pnl == pytest.approx(-RISK)
    assert broker.balance == pytest.approx(START - RISK)


async def test_a_target_hit_pays_two_r():
    broker = make_broker()
    await broker.open_position(make_plan(), ENTRY)

    fill = (await broker.sync(TARGET))[0]

    assert fill.reason is FillReason.TAKE_PROFIT
    assert fill.realised_pnl == pytest.approx(2 * RISK)
    assert broker.balance == pytest.approx(START + 2 * RISK)


async def test_shorts_settle_the_same_way():
    broker = make_broker()
    plan = make_plan(side=Side.SHORT, entry=ENTRY, stop=64_000.0, target=61_000.0)

    await broker.open_position(plan, ENTRY)
    fill = (await broker.sync(61_000.0))[0]

    assert fill.reason is FillReason.TAKE_PROFIT
    assert fill.realised_pnl == pytest.approx(2 * RISK)


# --- fills are not free ---------------------------------------------------


async def test_a_round_trip_at_the_same_price_still_loses_money():
    """The whole point of paper mode being honest: trading costs something."""
    broker = make_broker(slippage=0.001, fee_bps=4.5)

    await broker.open_position(make_plan(), ENTRY)
    fill = await broker.close_position(ENTRY)

    assert fill.realised_pnl < 0
    assert broker.balance < START


async def test_entry_pays_up_and_exit_takes_less():
    broker = make_broker(slippage=0.01)
    await broker.open_position(make_plan(), ENTRY)
    assert broker.state.entry_price == pytest.approx(ENTRY * 1.01)

    fill = await broker.close_position(ENTRY)
    assert fill.price == pytest.approx(ENTRY * 0.99)


async def test_a_short_entry_is_filled_below_the_reference():
    broker = make_broker(slippage=0.01)
    await broker.open_position(make_plan(side=Side.SHORT, stop=64_000.0, target=61_000.0), ENTRY)
    assert broker.state.entry_price == pytest.approx(ENTRY * 0.99)


async def test_fees_are_charged_on_entry_and_on_exit():
    broker = make_broker(fee_bps=4.5)
    entry_fill = await broker.open_position(make_plan(), ENTRY)
    assert entry_fill.fee > 0
    assert broker.balance == pytest.approx(START - entry_fill.fee)

    exit_fill = await broker.close_position(ENTRY)
    assert exit_fill.fee > 0
    # The reported result covers both legs, so it matches the balance movement.
    assert exit_fill.realised_pnl == pytest.approx(-(entry_fill.fee + exit_fill.fee))
    assert broker.balance == pytest.approx(START + exit_fill.realised_pnl)


# --- honest exits ---------------------------------------------------------


async def test_a_gap_through_the_stop_costs_more_than_the_planned_risk():
    """Stops fill at the market, not at the price you wished for."""
    broker = make_broker()
    await broker.open_position(make_plan(), ENTRY)

    fill = (await broker.sync(60_000.0))[0]  # gapped 2,000 past the 62,000 stop

    assert fill.price == pytest.approx(60_000.0)
    assert fill.realised_pnl == pytest.approx(-150.0)  # 3x the intended 50
    assert fill.realised_pnl < -RISK


async def test_overshooting_the_target_pays_no_bonus():
    """A resting limit fills at its own price, however far past it the market goes."""
    broker = make_broker()
    await broker.open_position(make_plan(), ENTRY)

    fill = (await broker.sync(70_000.0))[0]

    assert fill.price == pytest.approx(TARGET)
    assert fill.realised_pnl == pytest.approx(2 * RISK)


async def test_nothing_triggers_between_the_levels():
    broker = make_broker()
    await broker.open_position(make_plan(), ENTRY)
    assert await broker.sync(63_500.0) == []
    assert (await broker.managed_position()) is not None


async def test_sync_is_a_no_op_when_flat():
    assert await make_broker().sync(63_000.0) == []


# --- account state --------------------------------------------------------


async def test_equity_moves_with_the_mark_while_margin_stays_reserved():
    broker = make_broker()
    await broker.set_leverage(5, MarginMode.ISOLATED)
    await broker.open_position(make_plan(), ENTRY)
    await broker.sync(63_500.0)  # +500 * 0.05 = +25 unrealised

    state = await broker.account_state()
    assert state.account_value == pytest.approx(START + 25.0)
    assert state.total_margin_used == pytest.approx(0.05 * ENTRY / 5)
    assert state.withdrawable == pytest.approx(START - state.total_margin_used)

    position = state.position_for("BTC")
    assert position.size == pytest.approx(0.05)
    assert position.unrealized_pnl == pytest.approx(25.0)
    assert position.liquidation_price < ENTRY


async def test_a_flat_account_is_all_cash():
    state = await make_broker().account_state()
    assert state.account_value == pytest.approx(START)
    assert state.withdrawable == pytest.approx(START)
    assert state.positions == ()


async def test_leverage_above_the_asset_maximum_is_refused():
    with pytest.raises(BrokerError, match="40x"):
        await make_broker().set_leverage(50, MarginMode.ISOLATED)


async def test_the_mode_is_paper():
    assert make_broker().mode is TradingMode.PAPER


# --- one position at a time -----------------------------------------------


async def test_a_second_entry_is_refused_while_one_is_open():
    broker = make_broker()
    await broker.open_position(make_plan(), ENTRY)
    with pytest.raises(BrokerError, match="already holding"):
        await broker.open_position(make_plan(), ENTRY)


async def test_closing_when_flat_returns_nothing():
    assert await make_broker().close_position(ENTRY) is None


# --- persistence and restart ----------------------------------------------


async def test_a_position_survives_a_restart(conn):
    """Kill the app mid-trade and the position must still be there."""
    first = make_broker(conn)
    await first.set_leverage(5, MarginMode.ISOLATED)
    await first.open_position(make_plan(), ENTRY)

    revived = make_broker(conn, balance=99_999.0)  # a different default must not win
    held = await revived.managed_position()

    assert held is not None
    assert held.position.size == pytest.approx(0.05)
    assert held.position.entry_price == pytest.approx(ENTRY)
    assert held.stop_price == pytest.approx(STOP)
    assert held.take_profit_price == pytest.approx(TARGET)
    assert revived.balance == pytest.approx(START)

    # And it can still be managed to completion by the new instance.
    fill = (await revived.sync(STOP))[0]
    assert fill.realised_pnl == pytest.approx(-RISK)


async def test_balance_survives_a_restart_when_flat(conn):
    first = make_broker(conn)
    await first.open_position(make_plan(), ENTRY)
    await first.sync(TARGET)

    revived = make_broker(conn)
    assert revived.balance == pytest.approx(START + 2 * RISK)
    assert await revived.managed_position() is None


async def test_reset_returns_the_account_to_a_clean_slate(conn):
    broker = make_broker(conn)
    await broker.open_position(make_plan(), ENTRY)
    await broker.sync(STOP)

    broker.reset(500.0)

    assert broker.balance == pytest.approx(500.0)
    assert await broker.managed_position() is None
    assert make_broker(conn).balance == pytest.approx(500.0)  # persisted


# --- the trades and statistics pages --------------------------------------


async def test_fills_are_recorded_for_the_trades_page(conn):
    broker = make_broker(conn)
    await broker.open_position(make_plan(), ENTRY)
    await broker.sync(TARGET)

    fills = list_fills(conn, mode=TradingMode.PAPER)
    assert [fill.reason for fill in fills] == [FillReason.TAKE_PROFIT, FillReason.ENTRY]
    assert all(fill.coin == "BTC" for fill in fills)


async def test_statistics_count_closed_round_trips_only(conn):
    broker = make_broker(conn)

    await broker.open_position(make_plan(), ENTRY)
    await broker.sync(TARGET)  # win
    await broker.open_position(make_plan(), ENTRY)
    await broker.sync(STOP)  # loss
    await broker.open_position(make_plan(), ENTRY)  # still open — not a result yet

    stats = statistics(conn, TradingMode.PAPER)
    assert stats.closed_trades == 2
    assert stats.wins == 1
    assert stats.losses == 1
    assert stats.win_rate == pytest.approx(0.5)
    assert stats.total_pnl == pytest.approx(RISK)  # +100 then -50
