"""Live execution, driven against fakes.

Real orders cannot be exercised in a test suite, so what is checked here is the
thing that can be checked without money: the exact payload sent to the exchange, and
what the broker concludes from the exchange's answers. Every number that decides how
much is bought, at what price, and where the stop goes is asserted.
"""

from dataclasses import replace

import pytest

from src.broker.base import Broker, BrokerError
from src.broker.live import LiveBroker
from src.core.models import AccountState, AssetMeta, FillReason, MarginMode, Position, Side
from src.core.sizing import PositionPlan, plan_position
from src.db import connect
from src.store import list_fills
from src.core.models import TradingMode

BTC = AssetMeta(name="BTC", asset_index=0, sz_decimals=5, max_leverage=40)
ADDRESS = "0x5eA3e82B3605201d09b349789feD24E30D76c41b"
MARK = 63_000.0


def ok(statuses):
    return {"status": "ok", "response": {"type": "order", "data": {"statuses": statuses}}}


def filled(size="0.05", price="63010.0", oid=101):
    return {"filled": {"totalSz": size, "avgPx": price, "oid": oid}}


class FakeExchange:
    """Records what was sent and replays canned answers."""

    def __init__(self):
        self.orders = []
        self.bulk = []
        self.leverage = []
        self.order_result = ok([filled()])
        self.bulk_result = ok([{"resting": {"oid": 201}}, {"resting": {"oid": 202}}])
        self.bulk_raises = None

    def order(self, name, is_buy, sz, limit_px, order_type, reduce_only=False, **kw):
        self.orders.append(
            {
                "coin": name, "is_buy": is_buy, "sz": sz, "limit_px": limit_px,
                "order_type": order_type, "reduce_only": reduce_only,
            }
        )
        return self.order_result

    def bulk_orders(self, orders, builder=None, grouping="na"):
        if self.bulk_raises:
            raise self.bulk_raises
        self.bulk.append({"orders": orders, "grouping": grouping})
        return self.bulk_result

    def update_leverage(self, leverage, name, is_cross=True):
        self.leverage.append((leverage, name, is_cross))
        return {"status": "ok"}


class FakeInfo:
    def __init__(self):
        self.position = None
        self.fills = []
        self.resting = []

    async def clearinghouse_state(self, address):
        return AccountState(
            account_value=1_000.0,
            withdrawable=900.0,
            total_margin_used=100.0 if self.position else 0.0,
            positions=(self.position,) if self.position else (),
        )

    async def user_fills(self, address):
        return self.fills

    async def open_orders(self, address):
        return self.resting


def long_position(size=0.05, entry=63_010.0):
    return Position(
        coin="BTC", size=size, entry_price=entry, liquidation_price=57_000.0,
        unrealized_pnl=0.0, margin_used=630.0, leverage=5,
    )


@pytest.fixture
def conn():
    connection = connect(":memory:")
    yield connection
    connection.close()


@pytest.fixture
def broker(conn):
    exchange, info = FakeExchange(), FakeInfo()
    live = LiveBroker(
        "BTC", BTC, exchange=exchange, info=info, account_address=ADDRESS,
        slippage=0.01, conn=conn, clock=lambda: 1_700_000_000_000,
    )
    live.fake_exchange = exchange
    live.fake_info = info
    return live


def make_plan(side=Side.LONG, entry=MARK, stop=62_000.0, target=65_000.0) -> PositionPlan:
    plan = plan_position(
        side=side, entry_price=entry, stop_price=stop, take_profit_price=target,
        risk_usdc=50.0, equity_usdc=10_000.0, leverage=5, asset=BTC,
    )
    assert isinstance(plan, PositionPlan), plan
    return plan


# --- entering -------------------------------------------------------------


async def test_entry_is_an_ioc_limit_priced_through_the_book(broker):
    """Hyperliquid has no market order type; this is what one is."""
    await broker.open_position(make_plan(), MARK)

    sent = broker.fake_exchange.orders[0]
    assert sent["coin"] == "BTC"
    assert sent["is_buy"] is True
    assert sent["sz"] == 0.05
    assert sent["order_type"] == {"limit": {"tif": "Ioc"}}
    assert sent["reduce_only"] is False
    assert sent["limit_px"] == 63_630.0  # 1% above the mark, rounded to the grid


async def test_a_short_entry_sells_below_the_mark(broker):
    await broker.open_position(make_plan(Side.SHORT, stop=64_000.0, target=61_000.0), MARK)

    sent = broker.fake_exchange.orders[0]
    assert sent["is_buy"] is False
    assert sent["limit_px"] == 62_370.0  # 1% below


async def test_the_recorded_fill_is_what_executed_not_what_was_asked(broker):
    """The limit was 63,630 but it crossed at 63,010; the trade log must say so."""
    fill = await broker.open_position(make_plan(), MARK)

    assert fill.price == 63_010.0
    assert fill.size == 0.05
    assert fill.reason is FillReason.ENTRY


async def test_the_entry_fee_is_read_back_from_the_fill_record(broker):
    """The order response carries no fee, so an unread entry would look free."""
    broker.fake_info.fills = [{"oid": 101, "fee": "1.42", "coin": "BTC"}]

    fill = await broker.open_position(make_plan(), MARK)

    assert fill.fee == pytest.approx(1.42)


async def test_an_order_that_does_not_fill_is_an_error(broker):
    """An IOC that crosses nothing comes back cancelled. Recording a position that
    does not exist would leave the bot managing thin air."""
    broker.fake_exchange.order_result = ok([{"error": "Order could not immediately match"}])

    with pytest.raises(BrokerError, match="could not enter"):
        await broker.open_position(make_plan(), MARK)


async def test_a_resting_response_is_also_an_error(broker):
    broker.fake_exchange.order_result = ok([{"resting": {"oid": 9}}])

    with pytest.raises(BrokerError, match="did not fill"):
        await broker.open_position(make_plan(), MARK)


async def test_an_exchange_level_rejection_is_surfaced_verbatim(broker):
    """This is the real shape, captured from testnet: a signed action that the
    exchange parsed and then refused on its merits. It must not be mistaken for a
    fill, and the reason has to reach the user unedited."""
    broker.fake_exchange.order_result = {
        "status": "err",
        "response": "User or API Wallet 0x9454a9a0 does not exist.",
    }

    with pytest.raises(BrokerError, match="does not exist"):
        await broker.open_position(make_plan(), MARK)


async def test_a_rejected_leverage_change_is_not_silently_ignored(broker):
    broker.fake_exchange.update_leverage = lambda *a, **k: {
        "status": "err", "response": "User or API Wallet does not exist."
    }

    with pytest.raises(BrokerError, match="set leverage"):
        await broker.set_leverage(2, MarginMode.ISOLATED)


async def test_a_second_entry_is_refused_while_the_exchange_holds_one(broker):
    broker.fake_info.position = long_position()

    with pytest.raises(BrokerError, match="already holding"):
        await broker.open_position(make_plan(), MARK)


# --- the exits ------------------------------------------------------------


async def test_the_exits_are_reduce_only_and_tied_to_the_position(broker):
    """Sent separately, a leftover trigger could later open a brand-new position."""
    await broker.open_position(make_plan(), MARK)

    batch = broker.fake_exchange.bulk[0]
    assert batch["grouping"] == "positionTpsl"
    assert len(batch["orders"]) == 2
    for order in batch["orders"]:
        assert order["reduce_only"] is True
        assert order["is_buy"] is False  # closing a long sells
        assert order["sz"] == 0.05


async def test_the_stop_is_a_market_trigger_and_the_target_a_limit(broker):
    """Getting out matters more than the price; the target mirrors what the paper
    broker and the backtester assume, so results stay comparable."""
    await broker.open_position(make_plan(), MARK)
    stop, target = broker.fake_exchange.bulk[0]["orders"]

    assert stop["order_type"]["trigger"] == {
        "triggerPx": 62_000.0, "isMarket": True, "tpsl": "sl"
    }
    assert stop["limit_px"] < 62_000.0  # priced through, so it gets out

    assert target["order_type"]["trigger"] == {
        "triggerPx": 65_000.0, "isMarket": False, "tpsl": "tp"
    }
    assert target["limit_px"] == 65_000.0


async def test_a_short_places_its_exits_on_the_buy_side(broker):
    await broker.open_position(make_plan(Side.SHORT, stop=64_000.0, target=61_000.0), MARK)

    for order in broker.fake_exchange.bulk[0]["orders"]:
        assert order["is_buy"] is True


async def test_a_failure_to_place_the_exits_is_loud(broker):
    """The position is open and unprotected; that has to reach the user."""
    broker.fake_exchange.bulk_raises = RuntimeError("rate limited")

    with pytest.raises(BrokerError, match="UNPROTECTED"):
        await broker.open_position(make_plan(), MARK)


# --- exits the exchange takes ---------------------------------------------


async def test_a_triggered_stop_is_recognised_by_its_order_id(broker):
    await broker.open_position(make_plan(), MARK)
    broker.fake_info.position = long_position()
    await broker.sync(MARK)  # still holding

    broker.fake_info.position = None
    broker.fake_info.fills = [
        {
            "coin": "BTC", "oid": 201, "dir": "Close Long", "px": "62000.0",
            "sz": "0.05", "fee": "1.40", "closedPnl": "-50.5",
            "time": 1_700_000_100_000,
        }
    ]

    fills = await broker.sync(62_000.0)

    assert len(fills) == 1
    assert fills[0].reason is FillReason.STOP_LOSS
    assert fills[0].price == 62_000.0
    assert fills[0].realised_pnl == pytest.approx(-50.5 - 1.40)


async def test_a_filled_target_is_recognised_by_its_order_id(broker):
    await broker.open_position(make_plan(), MARK)
    broker.fake_info.position = None
    broker.fake_info.fills = [
        {
            "coin": "BTC", "oid": 202, "dir": "Close Long", "px": "65000.0",
            "sz": "0.05", "fee": "1.60", "closedPnl": "99.5",
            "time": 1_700_000_100_000,
        }
    ]

    fills = await broker.sync(65_000.0)

    assert fills[0].reason is FillReason.TAKE_PROFIT


async def test_a_close_nobody_here_ordered_is_not_guessed_at(broker):
    """Closed by hand on the exchange, or liquidated. Named for what is known."""
    await broker.open_position(make_plan(), MARK)
    broker.fake_info.position = None
    broker.fake_info.fills = [
        {
            "coin": "BTC", "oid": 999, "dir": "Close Long", "px": "61500.0",
            "sz": "0.05", "fee": "1.40", "closedPnl": "-75.0",
            "time": 1_700_000_100_000,
        }
    ]

    fills = await broker.sync(61_500.0)

    assert fills[0].reason is FillReason.MANUAL_CLOSE


async def test_a_close_is_reported_once_not_every_tick(broker):
    await broker.open_position(make_plan(), MARK)
    broker.fake_info.position = None
    broker.fake_info.fills = [
        {
            "coin": "BTC", "oid": 201, "dir": "Close Long", "px": "62000.0",
            "sz": "0.05", "fee": "1.40", "closedPnl": "-50.5",
            "time": 1_700_000_100_000,
        }
    ]

    assert len(await broker.sync(62_000.0)) == 1
    assert await broker.sync(62_000.0) == []
    assert await broker.sync(62_000.0) == []


async def test_holding_reports_nothing(broker):
    await broker.open_position(make_plan(), MARK)
    broker.fake_info.position = long_position()

    assert await broker.sync(63_500.0) == []


# --- closing on request ---------------------------------------------------


async def test_close_position_sends_a_reduce_only_ioc_the_other_way(broker):
    broker.fake_info.position = long_position()
    broker.fake_exchange.order_result = ok([filled(price="62800.0", oid=301)])

    fill = await broker.close_position(MARK)

    sent = broker.fake_exchange.orders[-1]
    assert sent["is_buy"] is False
    assert sent["reduce_only"] is True
    assert sent["limit_px"] == 62_370.0  # 1% below the mark
    assert fill.reason is FillReason.MANUAL_CLOSE
    assert fill.price == 62_800.0


async def test_closing_when_flat_returns_nothing(broker):
    assert await broker.close_position(MARK) is None


# --- account and leverage -------------------------------------------------


async def test_leverage_above_the_asset_maximum_never_reaches_the_exchange(broker):
    with pytest.raises(BrokerError, match="40x"):
        await broker.set_leverage(50, MarginMode.ISOLATED)
    assert broker.fake_exchange.leverage == []


async def test_leverage_is_sent_with_the_margin_mode(broker):
    await broker.set_leverage(5, MarginMode.ISOLATED)
    assert broker.fake_exchange.leverage == [(5, "BTC", False)]

    await broker.set_leverage(3, MarginMode.CROSS)
    assert broker.fake_exchange.leverage[-1] == (3, "BTC", True)


async def test_the_account_is_read_from_the_exchange(broker):
    state = await broker.account_state()
    assert state.account_value == 1_000.0
    assert broker.mode is TradingMode.LIVE


async def test_the_broker_reports_a_balance(broker):
    """Regression: only the paper broker had `balance`, and both the dashboard
    refresh and the console's closing summary read it off whichever broker they
    were handed. The first time Live was switched on, the UI raised AttributeError
    once a second and stopped updating."""
    await broker.account_state()
    assert broker.balance == 1_000.0


async def test_the_balance_takes_out_unrealised_profit(broker):
    """`balance` means realised cash in the paper broker. Hyperliquid reports
    account value with the open position's profit already in it, so the two would
    otherwise mean different things under one name."""
    broker.fake_info.position = long_position()
    broker.fake_info.position = replace(broker.fake_info.position, unrealized_pnl=120.0)

    await broker.account_state()

    assert broker.balance == pytest.approx(880.0)


def test_every_broker_must_report_a_balance():
    """The interface, not just the two implementations - so the next broker cannot
    repeat this."""
    assert "balance" in Broker.__abstractmethods__


# --- surviving a restart --------------------------------------------------


async def test_the_exits_are_recovered_from_the_exchange_after_a_restart(broker):
    """Nothing local survives, but the exchange still holds the resting triggers."""
    broker.fake_info.position = long_position()
    broker.fake_info.resting = [
        {"coin": "BTC", "isTrigger": True, "triggerPx": "62000.0"},
        {"coin": "BTC", "isTrigger": True, "triggerPx": "65000.0"},
    ]

    held = await broker.managed_position()

    assert held is not None
    assert held.stop_price == 62_000.0  # a long's stop is the lower one
    assert held.take_profit_price == 65_000.0


async def test_a_shorts_recovered_stop_is_the_higher_level(broker):
    broker.fake_info.position = long_position(size=-0.05)
    broker.fake_info.resting = [
        {"coin": "BTC", "isTrigger": True, "triggerPx": "61000.0"},
        {"coin": "BTC", "isTrigger": True, "triggerPx": "64000.0"},
    ]

    held = await broker.managed_position()

    assert held.stop_price == 64_000.0
    assert held.take_profit_price == 61_000.0


# --- the record -----------------------------------------------------------


async def test_live_fills_are_recorded_separately_from_paper(broker, conn):
    await broker.open_position(make_plan(), MARK)

    assert len(list_fills(conn, mode=TradingMode.LIVE)) == 1
    assert list_fills(conn, mode=TradingMode.PAPER) == []
