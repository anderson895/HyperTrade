"""Settings page — two cards under a titled header, following the reference design.

Account on the left, Trading on the right as a two-column grid. What a given risk
and leverage pair would actually produce is shown on the About page instead: it is
reference material, consulted once, not something to read on every edit.
"""

from __future__ import annotations

import qtawesome as qta
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import secrets_store
from ..config import AppSettings
from ..core.models import MarginMode, Network, Timeframe, TradingMode
from . import theme
from .chrome import PageHeader
from .widgets import NOTE_WIDTH, TitledCard, note_label, wrapped_label


def _secret_field() -> tuple[QWidget, QLineEdit]:
    """A masked input with an eye toggle. Blank means "keep whatever is stored"."""
    edit = QLineEdit()
    edit.setEchoMode(QLineEdit.EchoMode.Password)
    edit.setPlaceholderText(
        "Stored - leave blank to keep it" if secrets_store.has_agent_key() else "0x..."
    )

    eye = QToolButton()
    eye.setIcon(qta.icon("fa6s.eye", color=theme.MUTED))
    eye.setCheckable(True)
    eye.setToolTip("Show the key")

    def toggle(shown: bool) -> None:
        edit.setEchoMode(QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password)
        eye.setIcon(qta.icon("fa6s.eye-slash" if shown else "fa6s.eye", color=theme.MUTED))

    eye.toggled.connect(toggle)

    container = QWidget()
    row = QHBoxLayout(container)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(6)
    row.addWidget(edit)
    row.addWidget(eye)
    return container, edit


class _ProblemBox(QFrame):
    """Why the settings would not save, as a red callout rather than loose text.

    Every problem is listed at once. Fixing one field only to be told about the next
    is how an address and a key stayed swapped for three attempts: the key error hid
    the address error behind it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setProperty("errorbox", True)

        icon = QLabel()
        icon.setPixmap(
            qta.icon("fa6s.triangle-exclamation", color=theme.RED).pixmap(14, 14)
        )
        icon.setStyleSheet("background: transparent")
        self._heading = QLabel()
        self._heading.setStyleSheet(
            f"color: {theme.RED}; font-weight: bold; background: transparent"
        )

        head = QHBoxLayout()
        head.setSpacing(6)
        head.addWidget(icon)
        head.addWidget(self._heading)
        head.addStretch()

        self._body = wrapped_label()
        self._body.setStyleSheet(f"color: {theme.RED_TEXT}; background: transparent")

        column = QVBoxLayout(self)
        column.setContentsMargins(12, 10, 12, 10)
        column.setSpacing(4)
        column.addLayout(head)
        column.addWidget(self._body)

    def show_problems(self, problems: list[str]) -> None:
        count = len(problems)
        self._heading.setText(
            "Cannot save - 1 problem" if count == 1 else f"Cannot save - {count} problems"
        )
        self._body.setText("\n".join(f"•  {problem}" for problem in problems))
        self.setVisible(True)

    def text(self) -> str:
        """Everything on show, heading included — this was a plain QLabel once, and
        callers that only want to know what it says should not have to care."""
        return f"{self._heading.text()}\n{self._body.text()}"


class SettingsPage(QWidget):
    saved = Signal(object)  # AppSettings
    resetPaper = Signal()  # noqa: N815 — Qt signal naming
    #: Something that changes what a trade would look like was just edited.
    previewRequested = Signal()  # noqa: N815 — Qt signal naming

    def __init__(self, settings: AppSettings) -> None:
        super().__init__()
        self._settings = settings

        left = QVBoxLayout()
        left.setSpacing(16)
        left.addWidget(self._build_account_card())
        left.addStretch()

        right = QVBoxLayout()
        right.setSpacing(16)
        right.addWidget(self._build_trading_card())
        right.addStretch()

        cards = QHBoxLayout()
        cards.setSpacing(16)
        cards.addLayout(left, 1)
        cards.addLayout(right, 1)

        inner = QWidget()
        column = QVBoxLayout(inner)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(18)
        column.addWidget(self._build_header())
        # Above the cards, not below them. Save is at the top of the page, so a
        # message under a scrolled-off form is a message nobody reads — the address
        # and key errors sat off-screen through three attempts at fixing them.
        column.addWidget(self.problem)
        column.addLayout(cards)
        column.addStretch()

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(inner)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self._scroll)

        # Populate before wiring the change signals: `load` moves every combo, and
        # each move would call back into `current()` before `_settings` exists.
        self.load(settings)
        self.mode.currentIndexChanged.connect(self._refresh_notes)
        self.timeframe.currentIndexChanged.connect(self._refresh_notes)

        # Anything that changes the shape of a trade re-asks for a preview.
        for widget in (self.timeframe, self.margin):
            widget.currentIndexChanged.connect(self.previewRequested)
        for widget in (self.risk, self.leverage):
            widget.valueChanged.connect(self.previewRequested)

    # --- construction ----------------------------------------------------

    def _build_header(self) -> PageHeader:
        header = PageHeader(
            "fa6s.gear", "Bot Settings", "Configure your trading bot preferences"
        )
        self.button_reset = QPushButton("  Reset Paper Account")
        self.button_reset.setIcon(qta.icon("fa6s.rotate-left", color=theme.MUTED))
        self.button_save = QPushButton("  Save Settings")
        self.button_save.setObjectName("accentBtn")
        self.button_save.setIcon(qta.icon("fa6s.floppy-disk", color="#04140c"))

        self.button_save.clicked.connect(self._emit_saved)
        self.button_reset.clicked.connect(self.resetPaper)

        header.add_action(self.button_reset)
        header.add_action(self.button_save)

        self.problem = _ProblemBox()
        self.problem.setVisible(False)
        return header

    def _build_account_card(self) -> TitledCard:
        card = TitledCard("fa6s.user", "Account Settings")

        self.mode = QComboBox()
        self.mode.addItem("Paper Simulated - no real money", TradingMode.PAPER)
        self.mode.addItem("Live - REAL MONEY on Hyperliquid", TradingMode.LIVE)
        card.field("Trading Account", self.mode)
        self.mode_note = card.note()

        self.network = QComboBox()
        self.network.addItem("Mainnet", Network.MAINNET)
        self.network.addItem("Testnet (faucet USDC)", Network.TESTNET)
        card.field("Network", self.network)

        self.address = QLineEdit()
        self.address.setPlaceholderText("0x... (42 characters)")
        self._address_label = card.field("Main Wallet Address", self.address)
        self._address_note = card.note(
            "The wallet holding your USDC on Hyperliquid. Read-only here - the bot "
            "never signs with this wallet's key."
        )

        self._key_row, self.agent_key = _secret_field()
        self._key_label = card.field("API Wallet (Agent) Key", self._key_row)
        self._key_note = card.note(
            "Paste the key an approved API wallet gives you - the long one, 64 "
            "characters, not the address beside it. An agent can place and cancel "
            "orders but CANNOT withdraw. Stored in Windows Credential Manager, never "
            "in a file, and redacted from the logs."
        )

        card.finish()
        return card

    def _build_trading_card(self) -> TitledCard:
        card = TitledCard("fa6s.chart-line", "Trading Configuration")
        card.start_grid()

        self.timeframe = QComboBox()
        for value in Timeframe:
            self.timeframe.addItem(value.label, value)
        card.grid_field("Timeframe", self.timeframe, 0)

        self.risk = QDoubleSpinBox()
        self.risk.setRange(0.1, 100_000.0)
        self.risk.setDecimals(2)
        self.risk.setSuffix(" USDC")
        card.grid_field("Risk Per Trade", self.risk, 1)

        self.leverage = QSpinBox()
        self.leverage.setRange(1, 40)
        self.leverage.setSuffix("x")
        card.grid_field("Leverage", self.leverage, 0)

        self.margin = QComboBox()
        self.margin.addItem("Isolated (safer)", MarginMode.ISOLATED)
        self.margin.addItem("Cross", MarginMode.CROSS)
        card.grid_field("Margin Mode", self.margin, 1)

        self.balance = QDoubleSpinBox()
        self.balance.setRange(1.0, 10_000_000.0)
        self.balance.setDecimals(2)
        self.balance.setSuffix(" USDC")
        card.grid_field("Paper Starting Balance", self.balance, 0)

        self.slippage = QDoubleSpinBox()
        self.slippage.setRange(0.01, 5.0)
        self.slippage.setDecimals(2)
        self.slippage.setSuffix(" %")
        card.grid_field("Slippage Allowance", self.slippage, 1)

        self.daily_loss = QDoubleSpinBox()
        self.daily_loss.setRange(0.0, 1_000_000.0)
        self.daily_loss.setDecimals(2)
        self.daily_loss.setSuffix(" USDC")
        self.daily_loss.setSpecialValueText("Off")
        card.grid_field("Daily Loss Limit", self.daily_loss, 0, span=2)

        self.timeframe_note = card.note()
        card.note(
            "Risk decides the position size; leverage only caps it. Too little "
            "leverage for the risk means no trades at all, which is why the card on "
            "the right sizes one for you."
        )

        self.news_auto = QCheckBox("Pause around high-impact US releases")
        self.news_auto.setToolTip(
            "CPI, FOMC, NFP and the like, read from an economic calendar.\n"
            "If the calendar cannot be reached the bot stands aside rather than "
            "guessing that the coast is clear."
        )
        card.field("News Events", self.news_auto)

        self.news_before = QSpinBox()
        self.news_before.setRange(0, 240)
        self.news_before.setSuffix(" min")
        self.news_after = QSpinBox()
        self.news_after.setRange(0, 240)
        self.news_after.setSuffix(" min")
        card.start_grid()
        card.grid_field("Pause Before", self.news_before, 0)
        card.grid_field("Pause After", self.news_after, 1)

        self.news_block = QCheckBox("No new trades today (economic data day)")
        self.news_block.setToolTip(
            "A manual override, on top of the calendar. Open positions keep their stops."
        )
        card.field("Manual Override", self.news_block)
        card.note(
            "Leverage through a data release is how accounts get liquidated - "
            "liquidity thins out and a stop fills wherever the next resting order "
            "happens to be. Existing positions are left alone with their stops in "
            "place; only new entries are held back."
        )
        self.news_auto.toggled.connect(self._refresh_news_fields)

        card.finish()
        return card

    # --- api -------------------------------------------------------------

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
        self.news_auto.setChecked(settings.news_blackout_enabled)
        self.news_before.setValue(settings.news_blackout_before_min)
        self.news_after.setValue(settings.news_blackout_after_min)
        self._refresh_news_fields()
        self.address.setText(settings.account_address)
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
        settings.news_blackout_enabled = self.news_auto.isChecked()
        settings.news_blackout_before_min = self.news_before.value()
        settings.news_blackout_after_min = self.news_after.value()
        settings.account_address = self.address.text().strip()
        return settings

    def set_max_leverage(self, maximum: int) -> None:
        """Read from the exchange's `meta`, never hardcoded."""
        self.leverage.setMaximum(maximum)

    def set_enabled(self, enabled: bool) -> None:
        """Locked while the bot runs, except the blackout controls.

        Those stay live because news is the one thing you may need to react to
        mid-session, and standing aside never puts money at risk.
        """
        for widget in (
            self.mode, self.network, self.timeframe, self.margin, self.risk,
            self.leverage, self.balance, self.slippage, self.daily_loss,
            self.address, self.agent_key, self.button_save, self.button_reset,
        ):
            widget.setEnabled(enabled)
        self.news_block.setEnabled(True)
        self.news_auto.setEnabled(True)
        self._refresh_news_fields()

    # --- internals -------------------------------------------------------

    def _refresh_news_fields(self) -> None:
        """The two windows mean nothing with the calendar off, so they grey out."""
        live = self.news_auto.isChecked()
        self.news_before.setEnabled(live)
        self.news_after.setEnabled(live)

    def _refresh_notes(self) -> None:
        live = self.mode.currentData() is TradingMode.LIVE
        if live:
            self.mode_note.setText(
                "REAL MONEY. Orders are signed and sent to Hyperliquid. Start on "
                "Testnet, then Mainnet at small risk and low leverage until a full "
                "entry-to-exit cycle reconciles with the Hyperliquid UI."
            )
            self.mode_note.setStyleSheet(f"color: {theme.RED}; background: transparent")
        else:
            self.mode_note.setText(
                "Your trades are simulated against real Hyperliquid prices, and pay "
                "the real taker fee and slippage."
            )
            self.mode_note.setStyleSheet(f"color: {theme.MUTED}; background: transparent")

        # The credentials are meaningless in Paper mode, and showing a key field
        # invites pasting a key where nothing needs one.
        for widget in (
            self._address_label, self.address, self._address_note,
            self._key_label, self._key_row, self._key_note,
        ):
            widget.setVisible(live)

        advisories = self.current().advisories()
        self.timeframe_note.setText(" ".join(advisories))
        self.timeframe_note.setVisible(bool(advisories))
        self.timeframe_note.setStyleSheet(f"color: {theme.AMBER}; background: transparent")

    def _emit_saved(self) -> None:
        problems: list[str] = []

        typed = self.agent_key.text().strip()
        if typed:
            try:
                # The key goes straight to the credential store. It is never put on
                # the settings object, which is persisted to SQLite in plain text.
                secrets_store.save_agent_key(typed)
                self.agent_key.clear()
                self.agent_key.setPlaceholderText("Stored - leave blank to keep it")
            except ValueError as exc:
                problems.append(str(exc))

        problems.extend(self.current().validate(has_agent_key=secrets_store.has_agent_key()))

        if problems:
            self.problem.show_problems(problems)
            # The box sits at the top of a form that scrolls, and Save is reachable
            # from anywhere in it. Without this the message appears off-screen and
            # the click reads as having done nothing at all.
            self._scroll.verticalScrollBar().setValue(0)
            return

        self.problem.setVisible(False)
        self._settings = self.current()
        self.saved.emit(self._settings)
