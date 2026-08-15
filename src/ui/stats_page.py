"""Statistics page — the record of closed round trips."""

from __future__ import annotations

import sqlite3

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ..core.models import TradingMode
from ..store import statistics
from . import theme
from .widgets import Card, wrapped_label

PANEL_WIDTH = 420


class StatsPage(QWidget):
    ROWS = ("Closed Trades", "Wins", "Losses", "Win Rate", "Total PnL", "Total Fees")

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self._conn = conn

        title = QLabel("Statistics")
        title.setProperty("accent", True)

        self._labels: dict[str, QLabel] = {}
        panel = Card()
        panel.setMaximumWidth(PANEL_WIDTH)
        column = QVBoxLayout(panel)
        column.setContentsMargins(16, 14, 16, 14)
        column.setSpacing(8)
        for key in self.ROWS:
            label = QLabel(f"{key}: -")
            label.setStyleSheet("font-size: 15px")
            self._labels[key] = label
            column.addWidget(label)

        note = wrapped_label(
            "Paper and live results are counted separately. A closed round trip is "
            "one result; an entry on its own is not."
        )
        note.setProperty("muted", True)
        note.setMaximumWidth(PANEL_WIDTH)

        root = QVBoxLayout(self)
        root.addWidget(title)
        root.addWidget(panel, alignment=Qt.AlignmentFlag.AlignLeft)
        root.addWidget(note, alignment=Qt.AlignmentFlag.AlignLeft)
        root.addStretch()

    def refresh(self, mode: TradingMode = TradingMode.PAPER) -> None:
        stats = statistics(self._conn, mode)
        self._labels["Closed Trades"].setText(f"Closed Trades: {stats.closed_trades}")
        self._labels["Wins"].setText(f"Wins: {stats.wins}")
        self._labels["Losses"].setText(f"Losses: {stats.losses}")
        self._labels["Win Rate"].setText(f"Win Rate: {stats.win_rate:.0%}")
        self._labels["Total Fees"].setText(f"Total Fees: {stats.total_fees:,.2f} USDC")

        colour = theme.GREEN if stats.total_pnl >= 0 else theme.RED
        self._labels["Total PnL"].setText(f"Total PnL: {stats.total_pnl:+,.2f} USDC")
        self._labels["Total PnL"].setStyleSheet(f"font-size: 15px; color: {colour}")
