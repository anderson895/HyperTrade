"""Fill history — the Trades page and the Statistics page read from here.

Paper and Live fills share one table, tagged by mode, so switching modes never mixes
simulated results into real ones when a query filters on it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .core.models import Fill, FillReason, Side, TradingMode


def record_fill(conn: sqlite3.Connection, mode: TradingMode, fill: Fill) -> int:
    cursor = conn.execute(
        "INSERT INTO fills (time_ms, mode, coin, side, size, price, fee, reason, realised_pnl) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        ),
    )
    conn.commit()
    return cursor.lastrowid


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


def statistics(conn: sqlite3.Connection, mode: TradingMode) -> Statistics:
    """Counts closed round trips only — an entry on its own has no result yet."""
    row = conn.execute(
        """
        SELECT
            COUNT(*)                                        AS closed,
            COALESCE(SUM(realised_pnl > 0), 0)              AS wins,
            COALESCE(SUM(realised_pnl <= 0), 0)             AS losses,
            COALESCE(SUM(realised_pnl), 0.0)                AS pnl
        FROM fills
        WHERE mode = ? AND realised_pnl IS NOT NULL
        """,
        (mode.value,),
    ).fetchone()
    fees = conn.execute(
        "SELECT COALESCE(SUM(fee), 0.0) FROM fills WHERE mode = ?", (mode.value,)
    ).fetchone()[0]

    return Statistics(
        closed_trades=row["closed"],
        wins=row["wins"],
        losses=row["losses"],
        total_pnl=row["pnl"],
        total_fees=fees,
    )
