"""Importing the wallet's own fill history from Hyperliquid.

The Trades page shows the record on the account, not only the record this bot kept.
Two things have to hold for that to be worth anything: importing twice must not
double the history, and an imported trade must not quietly become part of the bot's
own performance figures.
"""

import pytest

from src.core.models import FillReason, Side, TradingMode
from src.db import connect
from src.store import (
    fill_from_exchange,
    import_exchange_fills,
    list_fills_with_source,
    statistics,
)

# Shaped exactly like Hyperliquid's userFills, taken from a real response.
ENTRY = {
    "coin": "BTC", "px": "62428.0", "sz": "0.00136", "side": "B",
    "time": 1786810000000, "dir": "Open Long", "closedPnl": "0.0",
    "oid": 517232492392, "crossed": False, "fee": "0.038591", "tid": 1001,
}
STOPPED = {
    "coin": "BTC", "px": "61759.0", "sz": "0.00136", "side": "A",
    "time": 1786810779098, "dir": "Close Long", "closedPnl": "-0.9476",
    "oid": 517232495703, "crossed": True, "fee": "0.038591", "tid": 1002,
}
TARGETED = {
    "coin": "BTC", "px": "63239.0", "sz": "0.00136", "side": "A",
    "time": 1786811779098, "dir": "Close Short", "closedPnl": "1.1030",
    "oid": 517232495704, "crossed": True, "fee": "0.038591", "tid": 1003,
}
BY_HAND = {
    "coin": "BTC", "px": "63000.0", "sz": "0.00136", "side": "A",
    "time": 1786812779098, "dir": "Close Long", "closedPnl": "0.5000",
    "oid": 999999999, "crossed": True, "fee": "0.038591", "tid": 1004,
}

ORDER_TYPES = {
    517232492392: "Limit",
    517232495703: "Stop Market",
    517232495704: "Take Profit Limit",
    # 999999999 deliberately absent: an order the exchange no longer reports.
}


@pytest.fixture
def conn():
    connection = connect(":memory:")
    yield connection
    connection.close()


# --- reading one fill ------------------------------------------------------


def test_the_exit_reason_comes_from_the_order_not_the_fill():
    """Nothing in a fill says whether it was a stop, a target or someone pressing
    Close. The order behind it does, and the fill carries the oid to find it."""
    stop, _ = fill_from_exchange(STOPPED, ORDER_TYPES)
    target, _ = fill_from_exchange(TARGETED, ORDER_TYPES)

    assert stop.reason is FillReason.STOP_LOSS
    assert target.reason is FillReason.TAKE_PROFIT


def test_an_exit_with_no_known_order_is_not_dressed_up_as_a_stop():
    """Hyperliquid stops reporting old orders. Guessing would put a fabricated
    stop-loss in a record of real money."""
    fill, _ = fill_from_exchange(BY_HAND, ORDER_TYPES)
    assert fill.reason is FillReason.MANUAL_CLOSE


def test_the_direction_gives_the_side_and_whether_it_opened():
    entry, _ = fill_from_exchange(ENTRY, ORDER_TYPES)
    short_exit, _ = fill_from_exchange(TARGETED, ORDER_TYPES)

    assert entry.side is Side.LONG and entry.reason is FillReason.ENTRY
    assert short_exit.side is Side.SHORT


def test_an_entry_carries_no_result():
    """closedPnl is reported on both legs and only means anything on the closing
    one. Stored as 0.0 on an entry, every entry would count as a losing trade."""
    entry, _ = fill_from_exchange(ENTRY, ORDER_TYPES)
    assert entry.realised_pnl is None

    exit_fill, _ = fill_from_exchange(STOPPED, ORDER_TYPES)
    assert exit_fill.realised_pnl == pytest.approx(-0.9476)


def test_the_tid_is_what_comes_back_as_the_identity():
    _, tid = fill_from_exchange(ENTRY, ORDER_TYPES)
    assert tid == "1001"


# --- importing -------------------------------------------------------------


def test_importing_twice_does_not_double_the_history(conn):
    """The whole point of syncing on a tid. Pressing the button again is the most
    likely thing a user does when they are not sure it worked."""
    raw = [ENTRY, STOPPED, TARGETED]

    first = import_exchange_fills(conn, TradingMode.LIVE, raw, ORDER_TYPES)
    second = import_exchange_fills(conn, TradingMode.LIVE, raw, ORDER_TYPES)

    assert first == (3, 3)
    assert second == (0, 3)  # nothing new, and it still says what it saw
    assert len(list_fills_with_source(conn, mode=TradingMode.LIVE)) == 3


def test_a_later_sync_picks_up_only_what_is_new(conn):
    import_exchange_fills(conn, TradingMode.LIVE, [ENTRY], ORDER_TYPES)

    imported, seen = import_exchange_fills(
        conn, TradingMode.LIVE, [ENTRY, STOPPED], ORDER_TYPES
    )

    assert (imported, seen) == (1, 2)


def test_one_malformed_fill_does_not_abandon_the_rest(conn):
    """A feed that adds or renames a field must not cost the whole sync."""
    broken = {"coin": "BTC", "dir": "Open Long"}  # no px, sz, time or tid

    imported, seen = import_exchange_fills(
        conn, TradingMode.LIVE, [ENTRY, broken, STOPPED], ORDER_TYPES
    )

    assert imported == 2
    assert seen == 3  # the count is what was offered, not what was taken


def test_imported_fills_are_marked_as_synced(conn):
    from src.core.models import Fill
    from src.store import record_fill

    record_fill(
        conn, TradingMode.LIVE,
        Fill(1_700_000_000_000, "BTC", Side.LONG, 0.05, 63_000.0, 0.14, FillReason.ENTRY),
    )
    import_exchange_fills(conn, TradingMode.LIVE, [ENTRY], ORDER_TYPES)

    rows = list_fills_with_source(conn, mode=TradingMode.LIVE)
    sources = {synced for _, synced in rows}

    assert sources == {True, False}  # one of each, and they are distinguishable


# --- keeping the bot's record separate -------------------------------------


def test_statistics_leave_synced_trades_out_by_default(conn):
    """These figures get shown to whoever the bot runs for. A trade placed by hand
    on the same wallet must not land inside a win rate presented as the bot's."""
    from src.core.models import Fill
    from src.store import record_fill

    record_fill(
        conn, TradingMode.LIVE,
        Fill(
            1_700_000_000_000, "BTC", Side.LONG, 0.05, 63_000.0, 0.14,
            FillReason.TAKE_PROFIT, realised_pnl=10.0,
        ),
    )
    import_exchange_fills(conn, TradingMode.LIVE, [STOPPED, BY_HAND], ORDER_TYPES)

    bot_only = statistics(conn, TradingMode.LIVE)
    everything = statistics(conn, TradingMode.LIVE, include_synced=True)

    assert bot_only.closed_trades == 1
    assert bot_only.total_pnl == pytest.approx(10.0)
    assert bot_only.win_rate == 1.0

    assert everything.closed_trades == 3
    assert everything.total_pnl == pytest.approx(10.0 - 0.9476 + 0.5)


def test_fees_are_split_the_same_way(conn):
    """A synced fill's fee is real money, but it is not a cost the bot incurred."""
    from src.core.models import Fill
    from src.store import record_fill

    record_fill(
        conn, TradingMode.LIVE,
        Fill(1_700_000_000_000, "BTC", Side.LONG, 0.05, 63_000.0, 1.0, FillReason.ENTRY),
    )
    import_exchange_fills(conn, TradingMode.LIVE, [ENTRY], ORDER_TYPES)

    assert statistics(conn, TradingMode.LIVE).total_fees == pytest.approx(1.0)
    assert statistics(
        conn, TradingMode.LIVE, include_synced=True
    ).total_fees == pytest.approx(1.038591)


def test_paper_history_is_untouched_by_a_live_sync(conn):
    from src.core.models import Fill
    from src.store import record_fill

    record_fill(
        conn, TradingMode.PAPER,
        Fill(1_700_000_000_000, "BTC", Side.LONG, 0.05, 63_000.0, 0.14, FillReason.ENTRY),
    )
    import_exchange_fills(conn, TradingMode.LIVE, [ENTRY, STOPPED], ORDER_TYPES)

    assert len(list_fills_with_source(conn, mode=TradingMode.PAPER)) == 1
    assert len(list_fills_with_source(conn, mode=TradingMode.LIVE)) == 2
