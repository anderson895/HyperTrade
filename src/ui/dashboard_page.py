"""Dashboard: status cards, BTC chart with position levels, recent logs."""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from dataclasses import dataclass

import qtawesome as qta
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.models import Timeframe, TradingMode
from ..db import get_ui_state, set_ui_state
from . import theme
from .chart import PriceChart
from .controller import Snapshot
from .widgets import Card, StatCard, StatusCard

RIGHT_COL_WIDTH = 340


@dataclass(frozen=True)
class ChartRange:
    """One entry in the time-range selector: how much history the chart shows.

    `timeframe` is the candle interval fetched to fill that span — a week of 5m
    candles would be 2,000 bars of mush, so longer spans use coarser candles, the
    same way an exchange chart does. `live` is the polled price drawn tick by tick
    and fetches nothing.

    These are spans, not candle sizes. "4H" means the last four hours; the Timeframe
    setting is what decides the candles the bot trades.
    """

    label: str
    timeframe: Timeframe | None
    count: int
    live: bool = False


def _ytd_days() -> int:
    now = dt.datetime.now(dt.timezone.utc)
    return (now - dt.datetime(now.year, 1, 1, tzinfo=dt.timezone.utc)).days + 2


CHART_RANGES: list[ChartRange] = [
    ChartRange("1s", None, 0, live=True),
    ChartRange("1H", Timeframe.M5, 12),
    ChartRange("4H", Timeframe.M5, 48),
    ChartRange("1D", Timeframe.M15, 96),
    ChartRange("1W", Timeframe.H1, 168),
    ChartRange("1M", Timeframe.H4, 180),
    ChartRange("YTD", Timeframe.D1, 0),
    ChartRange("All", Timeframe.W1, 400),
]

#: Opens on a day of 15-minute candles — enough shape to read, and it fills fast.
DEFAULT_RANGE = "1D"


def _range_index(label: str) -> int:
    """Index of a range by label, falling back to the default."""
    for index, entry in enumerate(CHART_RANGES):
        if entry.label == label:
            return index
    return next(i for i, e in enumerate(CHART_RANGES) if e.label == DEFAULT_RANGE)

LEVEL_COLORS = {
    logging.INFO: theme.TEXT,
    logging.WARNING: theme.AMBER,
    logging.ERROR: theme.RED,
    logging.CRITICAL: theme.RED,
}


class DashboardPage(QWidget):
    #: (interval, candle count) to fetch when the user picks a non-default range.
    chartRangeRequested = Signal(object, int)  # noqa: N815 — Qt signal naming

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        super().__init__()
        self._conn = conn

        # ---- Status cards row -------------------------------------------
        self.cards = {
            "internet": StatusCard("fa6s.globe", "Internet", "#3b82f6"),
            "market": StatusCard("fa6b.bitcoin", "Hyperliquid (BTC)", theme.BTC_ORANGE),
        }
        self.bot_card = StatCard("Bot Status", "STOPPED")
        self.bot_card.set_value("STOPPED", theme.RED)
        self.position_card = StatCard("Position", "Flat", "No open position")
        self.balance_card = StatCard("Paper Balance", "-", "Simulated - no real money")
        # Matches the logs column beneath it so the two right edges line up.
        self.balance_card.setFixedWidth(RIGHT_COL_WIDTH)

        cards_row = QHBoxLayout()
        cards_row.setSpacing(10)
        for card in self.cards.values():
            cards_row.addWidget(card, stretch=1)
        cards_row.addWidget(self.bot_card, stretch=1)
        cards_row.addWidget(self.position_card, stretch=1)
        cards_row.addWidget(self.balance_card)

        # ---- Chart panel --------------------------------------------------
        self._chart_title = QLabel("BTC-USD Perpetual (Hyperliquid)")
        self._chart_title.setProperty("accent", True)
        self._price_label = QLabel("$ -")
        self._price_label.setProperty("h1", True)
        self._pct_label = QLabel("")
        self._pct_label.setStyleSheet(f"color: {theme.MUTED}; font-size: 15px")

        self._style_combo = QComboBox()
        self._style_combo.addItem("Candles", "candles")
        self._style_combo.addItem("Line", "line")
        self._style_combo.setFixedWidth(96)
        self._style_combo.setToolTip(
            "Candles show the highs and lows the ATR stop is measured from."
        )

        self._range_combo = QComboBox()
        for entry in CHART_RANGES:
            self._range_combo.addItem(entry.label, entry)
        self._range_combo.setFixedWidth(80)
        self._range_combo.setToolTip(
            "How much history to show. These are spans, not candle sizes: '4H' means "
            "the last four hours, drawn with 5-minute candles. The candles the bot "
            "trades are set by Timeframe in Settings.\n\n"
            "'1s' is the live polled price. It starts empty because ticks cannot be "
            "backfilled."
        )

        price_row = QHBoxLayout()
        price_row.setSpacing(8)
        price_row.addWidget(self._price_label)
        price_row.addWidget(self._pct_label)
        price_row.addStretch()
        price_row.addWidget(self._style_combo)
        price_row.addWidget(self._range_combo)

        self.chart = PriceChart()
        self._range = CHART_RANGES[0]
        self._style_combo.currentIndexChanged.connect(self._on_style_changed)

        if self._conn is not None and get_ui_state(self._conn, "chart_style", "candles") == "line":
            self._style_combo.setCurrentIndex(1)  # fires _on_style_changed

        # A stale label from an older build falls back to the default rather than to
        # index 0, which is the live view and would open empty.
        saved = (
            get_ui_state(self._conn, "chart_range", DEFAULT_RANGE)
            if self._conn is not None
            else DEFAULT_RANGE
        )
        index = _range_index(saved)
        # Selected before the signal is wired, then applied once by hand:
        # setCurrentIndex does nothing when the value is already there, which would
        # leave the chart's mode out of step with the selection.
        self._range_combo.setCurrentIndex(index)
        self._range_combo.currentIndexChanged.connect(self._on_range_changed)
        self._on_range_changed(index)

        self._strategy_label = QLabel("Strategy: idle (press START BOT)")
        self._strategy_label.setProperty("muted", True)
        self._strategy_label.setWordWrap(True)

        chart_panel = Card()
        chart_col = QVBoxLayout(chart_panel)
        chart_col.setContentsMargins(14, 12, 14, 12)
        chart_col.addWidget(self._chart_title)
        chart_col.addLayout(price_row)
        chart_col.addWidget(self.chart, stretch=1)
        chart_col.addWidget(self._strategy_label)

        # ---- Recent logs panel --------------------------------------------
        logs_title = QLabel("Recent Logs")
        logs_title.setProperty("accent", True)
        clear_btn = QPushButton(" Clear")
        clear_btn.setIcon(qta.icon("fa6s.trash-can", color=theme.MUTED))
        clear_btn.clicked.connect(self._clear_logs)

        logs_head = QHBoxLayout()
        logs_head.addWidget(logs_title)
        logs_head.addStretch()
        logs_head.addWidget(clear_btn)

        self._log_list = QListWidget()
        self._log_list.setWordWrap(True)

        logs_panel = Card()
        logs_panel.setFixedWidth(RIGHT_COL_WIDTH)
        logs_col = QVBoxLayout(logs_panel)
        logs_col.setContentsMargins(14, 12, 14, 12)
        logs_col.addLayout(logs_head)
        logs_col.addWidget(self._log_list, stretch=1)

        # ---- Layout --------------------------------------------------------
        body_row = QHBoxLayout()
        body_row.setSpacing(10)
        body_row.addWidget(chart_panel, stretch=1)
        body_row.addWidget(logs_panel)

        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.addLayout(cards_row)
        root.addLayout(body_row, stretch=1)

        self._candles: list = []

    # ------------------------------------------------------------------ slots

    def apply(self, snapshot: Snapshot) -> None:
        self.cards["internet"].set_state(snapshot.connected)
        self.cards["market"].set_state(
            snapshot.connected, "Live price" if snapshot.connected else "No price feed"
        )

        state = "RUNNING" if snapshot.running else "STOPPED"
        self.bot_card.set_value(state, theme.GREEN if snapshot.running else theme.RED)

        if snapshot.mark:
            self._price_label.setText(f"${snapshot.mark:,.1f}")
            self.chart.set_mark(snapshot.mark)
            self._update_change(snapshot.mark)

        held = snapshot.position
        if held is None:
            self.position_card.set_value("Flat", theme.MUTED)
            self.position_card.set_sub(f"Free margin {snapshot.withdrawable:,.2f} USDC")
            self.chart.set_levels()
        else:
            position = held.position
            colour = theme.GREEN if position.side.value == "long" else theme.RED
            self.position_card.set_value(
                f"{position.side.value.upper()} {position.abs_size:g}", colour
            )
            self.position_card.set_sub(
                f"{position.unrealized_pnl:+,.2f} USDC  |  entry {position.entry_price:,.1f}"
            )
            self.chart.set_levels(
                position.entry_price, held.stop_price, held.take_profit_price
            )

        self.set_balance(snapshot.equity, snapshot.mode, snapshot.margin_used)

    def load_candles(self, candles, forming=None) -> None:
        self._candles = candles
        self.chart.load_candles(candles, forming)

    def _on_style_changed(self, index: int) -> None:
        style = self._style_combo.itemData(index)
        self.chart.set_style(style)
        if self._conn is not None:
            set_ui_state(self._conn, "chart_style", style)

    # --- the time range selector -----------------------------------------

    def current_request(self) -> tuple[Timeframe, int] | None:
        """The interval and count to fetch, or None for the live view, which is
        drawn from the polled price and fetches nothing."""
        if self._range.live:
            return None
        count = self._range.count or _ytd_days()
        return self._range.timeframe, count

    def _on_range_changed(self, index: int) -> None:
        self._range = self._range_combo.itemData(index)
        self.chart.set_mode("ticks" if self._range.live else "candles")

        # Candles built from one-second samples have no meaningful body.
        self._style_combo.setEnabled(not self._range.live)

        if self._range.live:
            title = "BTC-USD Perpetual - live price (polled each second)"
        else:
            title = (
                f"BTC-USD Perpetual - {self._range.label} view "
                f"({self._range.timeframe.label} candles)"
            )
        self._chart_title.setText(title)

        if self._conn is not None:
            set_ui_state(self._conn, "chart_range", self._range.label)

        request = self.current_request()
        if request is not None:
            self.chartRangeRequested.emit(*request)

    def _update_change(self, mark: float) -> None:
        """How far the forming candle has moved since the last one closed.

        Measured against the last close rather than a fixed 24-hour window, which
        would quietly mean a week on the weekly chart, and rather than the price when
        the app opened, which reads 0.00% for as long as anyone is watching.
        """
        if not self._candles or self._candles[-1].close <= 0:
            self._pct_label.setText("")
            return

        change = (mark / self._candles[-1].close - 1) * 100
        colour = theme.GREEN if change >= 0 else theme.RED
        self._pct_label.setText(f"{change:+.2f}% since last close")
        self._pct_label.setStyleSheet(f"color: {colour}; font-size: 15px; font-weight: bold")

    def set_balance(self, equity: float, mode: TradingMode, margin_used: float) -> None:
        if mode is TradingMode.LIVE:
            self.balance_card.set_title("Account Balance (LIVE)")
            self.balance_card.set_sub("Real USDC on Hyperliquid")
        else:
            self.balance_card.set_title("Paper Balance")
            self.balance_card.set_sub(
                f"Simulated - {margin_used:,.0f} USDC in margin"
                if margin_used
                else "Simulated - no real money"
            )
        self.balance_card.set_value(f"{equity:,.2f} USDC")

    def set_strategy_status(self, text: str) -> None:
        self._strategy_label.setText(f"Strategy: {text}")

    def add_log(self, message: str, level: int) -> None:
        """The panel is narrow, so it shows a short timestamp and the message only."""
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        item = QListWidgetItem(f"●  [{stamp}]  {message}")
        item.setForeground(QColor(LEVEL_COLORS.get(level, theme.TEXT)))
        self._log_list.insertItem(0, item)
        while self._log_list.count() > 200:
            self._log_list.takeItem(self._log_list.count() - 1)

    def _clear_logs(self) -> None:
        self._log_list.clear()
