"""Trades page — every fill, newest first.

Shows the record *on the wallet*, not only the record this bot kept, so a row can
have come from either. Which one is marked in the Source column: anything this bot
did not place itself was found on the exchange and says so.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Callable

import qtawesome as qta
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.models import TradingMode
from ..store import list_fills_with_source, statistics
from . import theme


class TradesPage(QWidget):
    COLUMNS = ("Time", "Coin", "Side", "Event", "Source", "Size", "Price", "Fee", "PnL")
    SOURCE_COLUMN = 4
    PNL_COLUMN = 8

    def __init__(
        self, conn: sqlite3.Connection, on_sync: Callable[[], None] | None = None
    ) -> None:
        super().__init__()
        self._conn = conn
        self._mode = TradingMode.PAPER

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        title = QLabel("Trades")
        title.setProperty("accent", True)
        self.summary = QLabel("")
        self.summary.setProperty("muted", True)

        # Only offered when a caller supplies the handler, which the window does
        # only in Live — a paper account has no wallet to read a history from.
        self.button_sync = QPushButton("  Sync from wallet")
        self.button_sync.setIcon(qta.icon("fa6s.rotate", color=theme.MUTED))
        self.button_sync.setToolTip(
            "Fetch this wallet's own fill history from Hyperliquid.\n"
            "Safe to run repeatedly - each fill has a unique id, so a second\n"
            "sync adds only what is new."
        )
        self._can_sync = on_sync is not None
        self.button_sync.setVisible(False)  # `reload` decides, once it knows the mode
        if on_sync is not None:
            self.button_sync.clicked.connect(on_sync)

        self.status = QLabel("")
        self.status.setProperty("muted", True)

        head = QHBoxLayout()
        head.addWidget(title)
        head.addSpacing(12)
        head.addWidget(self.button_sync)
        head.addWidget(self.status, stretch=1)
        head.addStretch()
        head.addWidget(self.summary)

        root = QVBoxLayout(self)
        root.addLayout(head)
        root.addWidget(self.table, stretch=1)

    def set_status(self, message: str, colour: str = theme.MUTED) -> None:
        self.status.setText(message)
        self.status.setStyleSheet(f"color: {colour}; background: transparent")

    def reload(self, mode: TradingMode | None = None) -> None:
        mode = self._mode if mode is None else mode
        self._mode = mode
        # A paper account has no wallet, so there is nothing to sync from. Offering
        # the button there would be a control whose only outcome is an error.
        self.button_sync.setVisible(self._can_sync and mode is TradingMode.LIVE)
        if mode is not TradingMode.LIVE:
            self.status.setText("")
        rows = list_fills_with_source(self._conn, mode=mode, limit=300)
        self.table.setRowCount(0)

        for fill, synced in rows:
            row = self.table.rowCount()
            self.table.insertRow(row)
            when = dt.datetime.fromtimestamp(fill.time_ms / 1000, dt.timezone.utc)
            values = (
                when.strftime("%m-%d %H:%M"),
                fill.coin,
                fill.side.value.upper(),
                fill.reason.value.replace("_", " "),
                "synced" if synced else "bot",
                f"{fill.size:g}",
                f"{fill.price:,.1f}",
                f"{fill.fee:.4f}",
                "" if fill.realised_pnl is None else f"{fill.realised_pnl:+,.2f}",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == self.PNL_COLUMN and fill.realised_pnl is not None:
                    item.setForeground(
                        QColor(theme.GREEN if fill.realised_pnl >= 0 else theme.RED)
                    )
                if column == self.SOURCE_COLUMN and synced:
                    item.setForeground(QColor(theme.MUTED))
                self.table.setItem(row, column, item)

        # Bot fills only, matching the Statistics page. A trade placed by hand on
        # the same wallet is part of the account's record but not of the bot's.
        stats = statistics(self._conn, mode)
        synced_rows = sum(1 for _, synced in rows if synced)
        extra = f"  |  {synced_rows} synced" if synced_rows else ""
        self.summary.setText(
            f"{stats.closed_trades} closed by the bot  |  {stats.wins} won "
            f"({stats.win_rate:.0%})  |  net {stats.total_pnl:+,.2f} USDC  |  "
            f"fees {stats.total_fees:,.2f} USDC{extra}"
        )
