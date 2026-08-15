"""Settings page — Bot Settings panel, laid out like PolyTrade Pro's.

Labels sit above their fields in one scrolling column rather than in a two-column
form, which keeps the long explanatory notes readable next to what they describe.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..config import AppSettings
from ..core.models import MarginMode, Network, Timeframe, TradingMode
from . import theme
from .widgets import Card

FORM_WIDTH = 620


class SettingsPage(QWidget):
    saved = Signal(object)  # AppSettings
    resetPaper = Signal()  # noqa: N815 — Qt signal naming

    def __init__(self, settings: AppSettings) -> None:
        super().__init__()
        self._settings = settings

        title = QLabel("Bot Settings")
        title.setProperty("accent", True)

        panel = Card()
        form = QVBoxLayout(panel)
        form.setContentsMargins(16, 14, 16, 16)
        form.setSpacing(6)

        def add_field(label: str, widget) -> QLabel:
            caption = QLabel(label)
            caption.setProperty("muted", True)
            caption.setContentsMargins(0, 8, 0, 0)
            form.addWidget(caption)
            form.addWidget(widget)
            return caption

        def add_note(text: str) -> QLabel:
            note = QLabel(text)
            note.setProperty("muted", True)
            note.setWordWrap(True)
            form.addWidget(note)
            return note

        # --- Trading mode ---------------------------------------------------
        self.mode = QComboBox()
        self.mode.addItem("Paper (simulated - no real money)", TradingMode.PAPER)
        self.mode.addItem("Live (REAL MONEY - Hyperliquid)", TradingMode.LIVE)
        add_field("Trading Mode", self.mode)
        self.mode_note = add_note("")

        # --- Network --------------------------------------------------------
        self.network = QComboBox()
        self.network.addItem("Mainnet", Network.MAINNET)
        self.network.addItem("Testnet (faucet USDC)", Network.TESTNET)
        add_field("Network", self.network)

        # --- Timeframe ------------------------------------------------------
        self.timeframe = QComboBox()
        for value in Timeframe:
            self.timeframe.addItem(value.label, value)
        add_field("Timeframe", self.timeframe)
        self.timeframe_note = add_note("")

        # --- Risk -----------------------------------------------------------
        self.risk = QDoubleSpinBox()
        self.risk.setRange(0.1, 100_000.0)
        self.risk.setDecimals(2)
        self.risk.setSuffix(" USDC")
        add_field("Risk Per Trade", self.risk)
        add_note(
            "How much you lose if the stop is hit. This decides the position size; "
            "leverage does not."
        )

        # --- Leverage -------------------------------------------------------
        self.leverage = QSpinBox()
        self.leverage.setRange(1, 40)
        self.leverage.setSuffix("x")
        add_field("Leverage", self.leverage)
        add_note(
            "A ceiling on position size, not a multiplier on risk. A trade needing "
            "more margin than this allows is rejected and logged, never resized."
        )

        # --- Margin mode ----------------------------------------------------
        self.margin = QComboBox()
        self.margin.addItem("Isolated (safer)", MarginMode.ISOLATED)
        self.margin.addItem("Cross", MarginMode.CROSS)
        add_field("Margin Mode", self.margin)

        # --- Paper balance --------------------------------------------------
        self.balance = QDoubleSpinBox()
        self.balance.setRange(1.0, 10_000_000.0)
        self.balance.setDecimals(2)
        self.balance.setSuffix(" USDC")
        add_field("Paper Starting Balance", self.balance)
        add_note(
            "Applies to a fresh paper account. An account already running keeps its "
            "balance - use Reset Paper Account to start over."
        )

        # --- Slippage -------------------------------------------------------
        self.slippage = QDoubleSpinBox()
        self.slippage.setRange(0.01, 5.0)
        self.slippage.setDecimals(2)
        self.slippage.setSuffix(" %")
        add_field("Slippage Allowance", self.slippage)

        # --- Daily loss limit -----------------------------------------------
        self.daily_loss = QDoubleSpinBox()
        self.daily_loss.setRange(0.0, 1_000_000.0)
        self.daily_loss.setDecimals(2)
        self.daily_loss.setSuffix(" USDC")
        self.daily_loss.setSpecialValueText("Off")
        add_field("Daily Loss Limit", self.daily_loss)
        add_note("Stops opening new trades for the rest of the UTC day once reached.")

        # --- News blackout --------------------------------------------------
        self.news_block = QCheckBox("No new trades today (economic data day)")
        self.news_block.setToolTip("CPI, FOMC, NFP. Open positions keep their stops.")
        add_field("News Events", self.news_block)
        add_note(
            "Leverage through a data release is how accounts get liquidated. Existing "
            "positions are left alone with their stops in place."
        )

        # --- Buttons ---------------------------------------------------------
        self.button_save = QPushButton("Save Settings")
        self.button_reset = QPushButton("Reset Paper Account")
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 14, 0, 0)
        buttons.addWidget(self.button_save)
        buttons.addWidget(self.button_reset)
        buttons.addStretch()
        form.addLayout(buttons)

        self.problem = QLabel("")
        self.problem.setWordWrap(True)
        self.problem.setStyleSheet(f"color: {theme.RED}")
        self.problem.setVisible(False)
        form.addWidget(self.problem)

        # Fixed, not maximum: a word-wrapped QLabel only reports its true height once
        # its width is settled. Left as a maximum, the card shrank to the widest
        # single widget and every explanatory note was clipped mid-sentence.
        panel.setFixedWidth(FORM_WIDTH)

        inner = QWidget()
        inner_col = QVBoxLayout(inner)
        inner_col.setContentsMargins(0, 0, 0, 0)
        inner_col.addWidget(title)
        inner_col.addWidget(panel, alignment=Qt.AlignmentFlag.AlignLeft)
        inner_col.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(scroll)

        self.button_save.clicked.connect(self._emit_saved)
        self.button_reset.clicked.connect(self.resetPaper)

        # Populate before wiring the change signals: `load` moves every combo, and
        # each move would call back into `current()` before `_settings` exists.
        self.load(settings)
        self.mode.currentIndexChanged.connect(self._refresh_notes)
        self.timeframe.currentIndexChanged.connect(self._refresh_notes)

    # ------------------------------------------------------------------ api

    def load(self, settings: AppSettings) -> None:
        self._settings = settings
        self.mode.setCurrentIndex(self.mode.findData(settings.trading_mode))
        self.network.setCurrentIndex(self.network.findData(settings.network))
        self.timeframe.setCurrentIndex(self.timeframe.findData(settings.timeframe))
        self.margin.setCurrentIndex(self.margin.findData(settings.margin_mode))
        self.risk.setValue(settings.risk_usdc)
        self.leverage.setValue(settings.leverage)
        self.balance.setValue(settings.paper_starting_balance)
        self.slippage.setValue(settings.slippage * 100)
        self.daily_loss.setValue(settings.daily_loss_limit_usdc)
        self.news_block.setChecked(settings.economic_data_day_block)
        self._refresh_notes()

    def current(self) -> AppSettings:
        settings = AppSettings(**vars(self._settings))
        settings.trading_mode = self.mode.currentData()
        settings.network = self.network.currentData()
        settings.timeframe = self.timeframe.currentData()
        settings.margin_mode = self.margin.currentData()
        settings.risk_usdc = self.risk.value()
        settings.leverage = self.leverage.value()
        settings.paper_starting_balance = self.balance.value()
        settings.slippage = self.slippage.value() / 100
        settings.daily_loss_limit_usdc = self.daily_loss.value()
        settings.economic_data_day_block = self.news_block.isChecked()
        return settings

    def set_max_leverage(self, maximum: int) -> None:
        """Read from the exchange's `meta`, never hardcoded."""
        self.leverage.setMaximum(maximum)

    def set_enabled(self, enabled: bool) -> None:
        """Locked while the bot runs, except the blackout toggle."""
        for widget in (
            self.mode, self.network, self.timeframe, self.margin, self.risk,
            self.leverage, self.balance, self.slippage, self.daily_loss,
            self.button_save, self.button_reset,
        ):
            widget.setEnabled(enabled)
        self.news_block.setEnabled(True)

    # -------------------------------------------------------------- internals

    def _refresh_notes(self) -> None:
        if self.mode.currentData() is TradingMode.LIVE:
            self.mode_note.setText(
                "Live trading is not implemented yet - the bot will refuse to start "
                "in Live mode."
            )
            self.mode_note.setStyleSheet(f"color: {theme.AMBER}")
        else:
            self.mode_note.setText(
                "Every trade is simulated against real Hyperliquid prices, and pays "
                "the real taker fee and slippage."
            )
            self.mode_note.setStyleSheet(f"color: {theme.MUTED}")

        advisories = self.current().advisories()
        self.timeframe_note.setText(" ".join(advisories))
        self.timeframe_note.setVisible(bool(advisories))
        self.timeframe_note.setStyleSheet(f"color: {theme.AMBER}")

    def _emit_saved(self) -> None:
        settings = self.current()
        problems = settings.validate()
        if problems:
            self.problem.setText("  ".join(problems))
            self.problem.setVisible(True)
            return
        self.problem.setVisible(False)
        self._settings = settings
        self.saved.emit(settings)
