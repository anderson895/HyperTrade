"""Trades page — every fill, newest first."""

from __future__ import annotations

import datetime as dt
import sqlite3

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.models import TradingMode
from ..store import list_fills, statistics
from . import theme


class TradesPage(QWidget):
    COLUMNS = ("Time", "Coin", "Side", "Event", "Size", "Price", "Fee", "PnL")
    PNL_COLUMN = 7

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self._conn = conn

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

        head = QHBoxLayout()
        head.addWidget(title)
        head.addStretch()
        head.addWidget(self.summary)

        root = QVBoxLayout(self)
        root.addLayout(head)
        root.addWidget(self.table, stretch=1)

    def reload(self, mode: TradingMode = TradingMode.PAPER) -> None:
        fills = list_fills(self._conn, mode=mode, limit=300)
        self.table.setRowCount(0)

        for fill in fills:
            row = self.table.rowCount()
            self.table.insertRow(row)
            when = dt.datetime.fromtimestamp(fill.time_ms / 1000, dt.timezone.utc)
            values = (
                when.strftime("%m-%d %H:%M"),
                fill.coin,
                fill.side.value.upper(),
                fill.reason.value.replace("_", " "),
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
                self.table.setItem(row, column, item)

        stats = statistics(self._conn, mode)
        self.summary.setText(
            f"{stats.closed_trades} closed  |  {stats.wins} won ({stats.win_rate:.0%})  |  "
            f"net {stats.total_pnl:+,.2f} USDC  |  fees {stats.total_fees:,.2f} USDC"
        )
