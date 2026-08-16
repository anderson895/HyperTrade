"""Fill history — the Trades page and the Statistics page read from here.

Paper and Live fills share one table, tagged by mode, so switching modes never mixes
simulated results into real ones when a query filters on it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .core.models import Fill, FillReason, Side, TradingMode


def record_fill(
    conn: sqlite3.Connection,
    mode: TradingMode,
    fill: Fill,
    *,
    exchange_id: str | None = None,
) -> int:
    """`exchange_id` marks a fill imported from the exchange rather than placed here.

    It is Hyperliquid's `tid`, unique per fill, and a partial unique index rejects a
    second insert of the same one — so re-syncing adds only what is new.
    """
    cursor = conn.execute(
        "INSERT INTO fills "
        "(time_ms, mode, coin, side, size, price, fee, reason, realised_pnl, exchange_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            fill.time_ms,
            mode.value,
            fill.coin,
            fill.side.value,
            fill.size,
            fill.price,
            fill.fee,
            fill.reason.value,
            fill.realised_pnl,
            exchange_id,
        ),
    )
    conn.commit()
    return cursor.lastrowid


#: What Hyperliquid's `orderType` means in this app's terms. An entry is decided by
#: the fill's direction, not the order type, because both legs of a trade can be
#: plain limits — only the exits carry a distinguishing type.
_EXIT_REASONS = {
    "Stop Market": FillReason.STOP_LOSS,
    "Stop Limit": FillReason.STOP_LOSS,
    "Take Profit Market": FillReason.TAKE_PROFIT,
    "Take Profit Limit": FillReason.TAKE_PROFIT,
}


def fill_from_exchange(raw: dict, order_types: dict[int, str]) -> tuple[Fill, str]:
    """One Hyperliquid fill as a `Fill`, plus its `tid`.

    `dir` is the exchange's own description — "Open Long", "Close Short" — and gives
    both the position side and whether this opened or closed it. The exit *reason*
    is not in the fill at all, so it is looked up from the order that produced it;
    an exit whose order is unknown falls back to a manual close rather than being
    presented as a stop or a target it may not have been.
    """
    direction = str(raw.get("dir", ""))
    side = Side.SHORT if "Short" in direction else Side.LONG
    opening = direction.startswith("Open")

    if opening:
        reason = FillReason.ENTRY
    else:
        kind = order_types.get(int(raw.get("oid", -1)), "")
        reason = _EXIT_REASONS.get(kind, FillReason.MANUAL_CLOSE)

    # `closedPnl` is reported on both legs and is only meaningful on the closing
    # one; an entry has no result yet, and storing 0.0 would count it as a losing
    # trade in the statistics.
    pnl = float(raw.get("closedPnl", 0.0) or 0.0) if not opening else None

    return (
        Fill(
            time_ms=int(raw["time"]),
            coin=str(raw["coin"]),
            side=side,
            size=abs(float(raw["sz"])),
            price=float(raw["px"]),
            fee=float(raw.get("fee", 0.0) or 0.0),
            reason=reason,
            realised_pnl=pnl,
        ),
        str(raw["tid"]),
    )


def import_exchange_fills(
    conn: sqlite3.Connection, mode: TradingMode, raw_fills: list[dict],
    order_types: dict[int, str],
) -> tuple[int, int]:
    """Insert whatever is new. Returns (imported, seen).

    Duplicates are rejected by the index rather than by a pre-check, so two syncs
    racing each other cannot both decide a fill is missing and insert it twice.
    """
    imported = 0
    for raw in raw_fills:
        try:
            fill, tid = fill_from_exchange(raw, order_types)
        except (KeyError, TypeError, ValueError):
            continue  # one malformed fill must not abandon the rest
        try:
            record_fill(conn, mode, fill, exchange_id=tid)
            imported += 1
        except sqlite3.IntegrityError:
            pass  # already have it
    return imported, len(raw_fills)


def _to_fill(row: sqlite3.Row) -> Fill:
    return Fill(
        time_ms=row["time_ms"],
        coin=row["coin"],
        side=Side(row["side"]),
        size=row["size"],
        price=row["price"],
        fee=row["fee"],
        reason=FillReason(row["reason"]),
        realised_pnl=row["realised_pnl"],
    )


def list_fills(
    conn: sqlite3.Connection,
    *,
    mode: TradingMode | None = None,
    limit: int | None = None,
) -> list[Fill]:
    """Most recent first."""
    query = "SELECT * FROM fills"
    params: list[object] = []
    if mode is not None:
        query += " WHERE mode = ?"
        params.append(mode.value)
    query += " ORDER BY time_ms DESC, id DESC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    return [_to_fill(row) for row in conn.execute(query, params)]


def list_fills_with_source(
    conn: sqlite3.Connection,
    *,
    mode: TradingMode | None = None,
    limit: int | None = None,
) -> list[tuple[Fill, bool]]:
    """As `list_fills`, but each fill is paired with whether it was synced.

    The Trades page shows the wallet's whole record, so it has to be able to say
    which rows this bot placed and which it merely found.
    """
    query = "SELECT * FROM fills"
    params: list[object] = []
    if mode is not None:
        query += " WHERE mode = ?"
        params.append(mode.value)
    query += " ORDER BY time_ms DESC, id DESC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    return [
        (_to_fill(row), row["exchange_id"] is not None)
        for row in conn.execute(query, params)
    ]


def clear_fills(conn: sqlite3.Connection, mode: TradingMode) -> None:
    """Used by 'Reset paper account'. Never offered for live history."""
    if mode is TradingMode.LIVE:
        raise ValueError("live fill history is a record of real money and is not erasable")
    conn.execute("DELETE FROM fills WHERE mode = ?", (mode.value,))
    conn.commit()


def realised_since(conn: sqlite3.Connection, mode: TradingMode, since_ms: int) -> float:
    """Realised profit from `since_ms` onward. Backs the daily loss limit."""
    return conn.execute(
        "SELECT COALESCE(SUM(realised_pnl), 0.0) FROM fills "
        "WHERE mode = ? AND realised_pnl IS NOT NULL AND time_ms >= ?",
        (mode.value, since_ms),
    ).fetchone()[0]


@dataclass(frozen=True)
class Statistics:
    closed_trades: int
    wins: int
    losses: int
    total_pnl: float
    total_fees: float

    @property
    def win_rate(self) -> float:
        return self.wins / self.closed_trades if self.closed_trades else 0.0

    @property
    def average_pnl(self) -> float:
        return self.total_pnl / self.closed_trades if self.closed_trades else 0.0


def statistics(
    conn: sqlite3.Connection, mode: TradingMode, *, include_synced: bool = False
) -> Statistics:
    """Counts closed round trips only — an entry on its own has no result yet.

    Synced fills are excluded by default. They are the wallet's record, not this
    bot's: a trade placed by hand on the same account would otherwise land in the
    win rate shown as the bot's performance, with nothing on screen saying so. The
    Statistics page offers the wider view as an explicit choice.
    """
    only_bot = "" if include_synced else " AND exchange_id IS NULL"
    row = conn.execute(
        f"""
        SELECT
            COUNT(*)                                        AS closed,
            COALESCE(SUM(realised_pnl > 0), 0)              AS wins,
            COALESCE(SUM(realised_pnl <= 0), 0)             AS losses,
            COALESCE(SUM(realised_pnl), 0.0)                AS pnl
        FROM fills
        WHERE mode = ? AND realised_pnl IS NOT NULL{only_bot}
        """,
        (mode.value,),
    ).fetchone()
    fees = conn.execute(
        f"SELECT COALESCE(SUM(fee), 0.0) FROM fills WHERE mode = ?{only_bot}",
        (mode.value,),
    ).fetchone()[0]

    return Statistics(
        closed_trades=row["closed"],
        wins=row["wins"],
        losses=row["losses"],
        total_pnl=row["pnl"],
        total_fees=fees,
    )
