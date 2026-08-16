"""Settings page — Account, Trading and Exit Rules under a titled header.

Trading is laid out as a two-column grid. What a given risk and leverage pair would
actually produce is shown on the About page instead: it is reference material,
consulted once, not something to read on every edit.
"""

from __future__ import annotations

import inspect
from dataclasses import replace

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
from ..strategy import available

#: One line each, keyed by registry name. Absent is fine — the note just stays
#: empty rather than the page inventing a description for a strategy it has
#: never heard of.
STRATEGY_NOTES = {
    "volume_rejection": (
        "Fades failed breakouts: price pushes past the 24-hour range on high "
        "volume, is rejected, and closes back inside. The stop sits past the "
        "rejection wick."
    ),
}
from . import theme
from .chrome import PageHeader
from .widgets import NOTE_WIDTH, TitledCard, note_label, wrapped_label


def _secret_field() -> tuple[QWidget, QLineEdit, QToolButton]:
    """A masked input with an eye toggle and a way to forget the stored key.

    Blank means "keep whatever is stored", which is the right default and also made
    the key unremovable: the only way to change it was to paste another over it, and
    there was no way at all to take it back out. Handing the app on, or revoking an
    agent, both need that.
    """
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

    forget = QToolButton()
    forget.setIcon(qta.icon("fa6s.trash-can", color=theme.RED))
    forget.setToolTip(
        "Remove the stored key from Windows Credential Manager.\n"
        "The bot cannot trade live without one. Nothing on Hyperliquid changes -\n"
        "revoke the agent there too if that is what you mean to do."
    )

    container = QWidget()
    row = QHBoxLayout(container)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(6)
    row.addWidget(edit)
    row.addWidget(eye)
    row.addWidget(forget)
    return container, edit, forget


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
    #: The stored agent key was deleted. Emitted before `saved`, so the window can
    #: say why the mode just changed rather than leaving it to be inferred.
    keyForgotten = Signal()  # noqa: N815 — Qt signal naming
    #: Something that changes what a trade would look like was just edited.
    previewRequested = Signal()  # noqa: N815 — Qt signal naming

    def __init__(self, settings: AppSettings) -> None:
        super().__init__()
        self._settings = settings

        left = QVBoxLayout()
        left.setSpacing(16)
        left.addWidget(self._build_account_card())
        # Under Account rather than inside Trading: the exits are a set of numbers
        # that belong together, and the Trading card is already dense.
        left.addWidget(self._build_exits_card())
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
        # Hidden in Live, where there is nothing it can do: `reset` exists only on
        # the paper broker, because a live balance is real money. Offered there it
        # crashed the task it ran in and showed the user nothing.
        self.button_reset = QPushButton("  Reset Paper Account")
        self.button_reset.setIcon(qta.icon("fa6s.rotate-left", color=theme.MUTED))
        self.button_reset.setToolTip(
            "Set the simulated balance back to the paper starting balance and "
            "clear its trade history. Paper mode only."
        )
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

        self._key_row, self.agent_key, self.button_forget_key = _secret_field()
        self.button_forget_key.clicked.connect(self._forget_agent_key)
        self._key_label = card.field("API Wallet (Agent) Key", self._key_row)
        self._key_note = card.note(
            "Paste the key an approved API wallet gives you - the long one, 64 "
            "characters, not the address beside it. An agent can place and cancel "
            "orders but CANNOT withdraw. Stored in Windows Credential Manager, never "
            "in a file, and redacted from the logs."
        )

        card.finish()
        return card

    def _build_exits_card(self) -> TitledCard:
        """Where a trade gets out: the target, and the stop that follows it up."""
        card = TitledCard("fa6s.right-from-bracket", "Exit Rules")
        card.start_grid()

        self.take_profit = QDoubleSpinBox()
        self.take_profit.setRange(0.1, 20.0)
        self.take_profit.setDecimals(1)
        self.take_profit.setSingleStep(0.5)
        self.take_profit.setSuffix(" R")
        self.take_profit.setToolTip(
            "Target as a multiple of the stop distance.\n"
            "2R means a 60,000 entry with a stop at 59,000 targets 62,000."
        )
        card.grid_field("Take Profit", self.take_profit, 0)

        self.stop_buffer = QDoubleSpinBox()
        self.stop_buffer.setRange(0.0, 20.0)
        self.stop_buffer.setDecimals(2)
        self.stop_buffer.setSingleStep(0.05)
        self.stop_buffer.setSuffix(" %")
        self.stop_buffer.setToolTip(
            "How far past the rejection wick the stop sits.\n"
            "Greyed out for any strategy that does not measure its stop from a wick."
        )
        card.grid_field("Stop Buffer", self.stop_buffer, 1)

        self.trail_activation = QDoubleSpinBox()
        self.trail_activation.setRange(0.0, 10.0)
        self.trail_activation.setDecimals(1)
        self.trail_activation.setSingleStep(0.5)
        self.trail_activation.setSuffix(" R")
        self.trail_activation.setToolTip(
            "Profit needed before the stop starts following.\n"
            "0 starts as soon as trailing would sit better than the original stop."
        )
        card.grid_field("Trail Activation", self.trail_activation, 0)

        self.trail_distance = QDoubleSpinBox()
        self.trail_distance.setRange(0.01, 20.0)
        self.trail_distance.setDecimals(2)
        self.trail_distance.setSingleStep(0.1)
        self.trail_distance.setSuffix(" %")
        self.trail_distance.setToolTip("How far behind the best price the stop trails.")
        card.grid_field("Trail Distance", self.trail_distance, 1)

        self.post_only = QCheckBox("Rest a maker order at the signal price")
        self.post_only.setToolTip(
            "Off: cross the spread and take the trade now, paying the taker fee.\n"
            "On: rest a post-only order and wait for price to come back. Cheaper,\n"
            "but the trade only happens if the pullback arrives - which is itself\n"
            "a filter, and changes which trades you get, not just what they cost."
        )
        card.field("Entry", self.post_only)

        self.entry_expiry = QSpinBox()
        self.entry_expiry.setRange(1, 96)
        self.entry_expiry.setSuffix(" candles")
        self.entry_expiry.setToolTip(
            "How long a resting entry waits before it is cancelled as stale."
        )
        card.field("Cancel Unfilled After", self.entry_expiry)
        self.post_only.toggled.connect(self._refresh_entry_fields)

        self.trailing = QCheckBox("Move the stop up behind a winning trade")
        card.field("Trailing Stop", self.trailing)
        card.note(
            "A trailing stop protects profit but takes you out earlier. Widening the "
            "stop or lowering the target changes how often you win and how much each "
            "win pays - measure it, do not guess: tools/run_backtest.py."
        )
        self.trailing.toggled.connect(self._refresh_trailing_fields)

        card.finish()
        return card

    def _build_trading_card(self) -> TitledCard:
        card = TitledCard("fa6s.chart-line", "Trading Configuration")

        # Shown, not chosen. One strategy ships, so a dropdown holding a single
        # item was a control that could not control anything. It still has to be
        # *stated* — a live account was once started on the wrong strategy, and
        # the fix for that was saying which one is loaded, not offering a choice.
        self.strategy_name = QLabel()
        self.strategy_name.setObjectName("strategyName")
        self.strategy_name.setStyleSheet("font-weight: 600; background: transparent")
        card.field("Strategy", self.strategy_name)
        self.strategy_note = card.note()

        card.start_grid()

        self.timeframe = QComboBox()
        for value in Timeframe:
            self.timeframe.addItem(value.label, value)
        card.grid_field("Timeframe", self.timeframe, 0)

        self.risk = QDoubleSpinBox()
        self.risk.setRange(0.01, 100_000.0)
        self.risk.setDecimals(2)
        self.risk_unit = QComboBox()
        self.risk_unit.addItem("USDC", "usdc")
        self.risk_unit.addItem("% of equity", "pct")
        self.risk_unit.setFixedWidth(104)
        self.risk_unit.setToolTip(
            "A fixed USDC amount stays the same as the account moves.\n"
            "A percentage compounds both ways - the stake grows with a winning\n"
            "account and shrinks with a losing one."
        )
        risk_row = QWidget()
        risk_layout = QHBoxLayout(risk_row)
        risk_layout.setContentsMargins(0, 0, 0, 0)
        risk_layout.setSpacing(6)
        risk_layout.addWidget(self.risk)
        risk_layout.addWidget(self.risk_unit)
        card.grid_field("Risk Per Trade", risk_row, 1)
        self.risk_unit.currentIndexChanged.connect(self._refresh_risk_unit)

        self.leverage = QSpinBox()
        self.leverage.setRange(1, 40)
        self.leverage.setSuffix("x")
        card.grid_field("Leverage", self.leverage, 0)

        self.margin = QComboBox()
        self.margin.addItem("Isolated (safer)", MarginMode.ISOLATED)
        self.margin.addItem("Cross", MarginMode.CROSS)
        self.margin.setToolTip(
            "Isolated caps a trade's loss at its own margin; Cross puts the whole\n"
            "balance behind it. Sent to Hyperliquid with the leverage.\n"
            "Live only - the paper broker does not simulate the difference."
        )
        # Hidden in Paper for the same reason the paper balance is hidden in Live:
        # `PaperBroker.set_leverage` takes the margin mode and ignores it, so in
        # Paper this is a control that controls nothing.
        self._margin_label = card.grid_field("Margin Mode", self.margin, 1)

        self.balance = QDoubleSpinBox()
        self.balance.setRange(1.0, 10_000_000.0)
        self.balance.setDecimals(2)
        self.balance.setSuffix(" USDC")
        # Hidden in Live mode, where it does nothing: the balance then comes from
        # the exchange. Left on screen it reads as a starting stake for real money,
        # which is the most alarming thing it could be mistaken for.
        self._balance_label = card.grid_field("Paper Starting Balance", self.balance, 0)

        self.slippage = QDoubleSpinBox()
        self.slippage.setRange(0.01, 5.0)
        self.slippage.setDecimals(2)
        self.slippage.setSuffix(" %")
        self.slippage.setToolTip(
            "How far through the book a market-style order may be priced.\n"
            "Hyperliquid has no market order, so this is what makes an IOC limit\n"
            "behave like one.\n"
            "Applies to Close position always, and to entries only when the\n"
            "maker order below is off - a resting order sits at its own price."
        )
        card.grid_field("Slippage Allowance", self.slippage, 1)

        # A percentage, not a USDC figure: an absolute limit does not travel. 2.00
        # USDC is a sensible circuit breaker on a 99 USDC account and halts a 1,000
        # USDC one after three trades.
        self.daily_loss = QDoubleSpinBox()
        self.daily_loss.setRange(0.0, 50.0)
        self.daily_loss.setDecimals(2)
        self.daily_loss.setSuffix(" % of equity")
        self.daily_loss.setSpecialValueText("Off")
        self.daily_loss.setToolTip(
            "Stop taking new entries once the day's realised losses reach this.\n"
            "An open position is left alone - it keeps the stop and target that\n"
            "are already with the exchange. Resets at 00:00 UTC."
        )
        card.grid_field("Daily Loss Limit", self.daily_loss, 0, span=2)
        self.daily_loss_note = card.note()

        self.timeframe_note = card.note()

        self.clamp_size = QCheckBox("Cap the size at the leverage limit")
        self.clamp_size.setToolTip(
            "Off: a trade that does not fit is refused, and the log says why.\n"
            "On: the size is cut down to what the leverage allows, so the trade\n"
            "goes on at a smaller risk than requested. The log says by how much."
        )
        card.field("When the risk will not fit", self.clamp_size)
        card.note(
            "Risk decides the position size; leverage only caps it. A tight stop "
            "needs a large position to risk the same amount, so a 0.2% stop and 3% "
            "risk needs about 17x whatever the balance is - cap the size and the "
            "real risk lands near 1.4%, refuse it and nothing trades."
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
        self.risk_unit.setCurrentIndex(1 if settings.risk_pct else 0)
        self.risk.setValue(
            settings.risk_pct * 100 if settings.risk_pct else settings.risk_usdc
        )
        self._refresh_risk_unit()
        self.clamp_size.setChecked(settings.clamp_size_to_leverage)
        self.leverage.setValue(settings.leverage)
        self.balance.setValue(settings.paper_starting_balance)
        self.slippage.setValue(settings.slippage * 100)
        self.daily_loss.setValue(settings.daily_loss_limit_pct * 100)
        # A config saved before this was a percentage still has its fixed limit in
        # force. Say so, rather than showing "Off" over a limit that is running.
        self.daily_loss_note.setText(
            f"A fixed limit of {settings.daily_loss_limit_usdc:,.2f} USDC is in "
            f"force from an earlier version. Saving replaces it with the percentage "
            f"above."
            if settings.daily_loss_limit_usdc > 0 and not settings.daily_loss_limit_pct
            else ""
        )
        self.news_block.setChecked(settings.economic_data_day_block)
        self.news_auto.setChecked(settings.news_blackout_enabled)
        self.news_before.setValue(settings.news_blackout_before_min)
        self.news_after.setValue(settings.news_blackout_after_min)
        self._refresh_news_fields()

        # A saved config naming a strategy this build no longer has used to be
        # recoverable by touching the dropdown. Without one, it would be a dead end:
        # `validate` refuses to start on an unknown strategy and there would be no
        # control left to change it. So it is normalised here instead, on the copy
        # `current()` builds from, and `_refresh_strategy_note` says what happened.
        if settings.strategy not in available():
            self._settings = replace(settings, strategy=AppSettings().strategy)
        self.take_profit.setValue(settings.take_profit_rr)
        self.stop_buffer.setValue(settings.stop_buffer_pct * 100)
        self.trailing.setChecked(settings.trailing_enabled)
        self.trail_activation.setValue(settings.trailing_activation_rr)
        self.trail_distance.setValue(settings.trailing_distance_pct * 100)
        self._refresh_trailing_fields()
        self.post_only.setChecked(settings.post_only_entry)
        self.entry_expiry.setValue(settings.entry_expiry_candles)
        self._refresh_entry_fields()
        # Also on load, not only when the dropdown is touched: the page opens on a
        # saved strategy, and the note and the greying describe *that* one.
        self._refresh_strategy_note()
        self.address.setText(settings.account_address)
        self._refresh_notes()

    def current(self) -> AppSettings:
        settings = AppSettings(**vars(self._settings))
        settings.trading_mode = self.mode.currentData()
        settings.network = self.network.currentData()
        settings.timeframe = self.timeframe.currentData()
        settings.margin_mode = self.margin.currentData()
        if self.risk_unit.currentData() == "pct":
            settings.risk_pct = self.risk.value() / 100
        else:
            settings.risk_pct = 0.0
            settings.risk_usdc = self.risk.value()
        settings.clamp_size_to_leverage = self.clamp_size.isChecked()
        settings.leverage = self.leverage.value()
        settings.paper_starting_balance = self.balance.value()
        settings.slippage = self.slippage.value() / 100
        # The percentage is authoritative once saved, so the legacy fixed limit is
        # cleared rather than left behind to take over if the percentage is zeroed.
        settings.daily_loss_limit_pct = self.daily_loss.value() / 100
        settings.daily_loss_limit_usdc = 0.0
        settings.economic_data_day_block = self.news_block.isChecked()
        settings.news_blackout_enabled = self.news_auto.isChecked()
        settings.news_blackout_before_min = self.news_before.value()
        settings.news_blackout_after_min = self.news_after.value()
        settings.account_address = self.address.text().strip()
        # `settings` is copied from `self._settings`, which `load` normalised, so
        # the strategy carries through without a widget to read it back from.
        settings.take_profit_rr = self.take_profit.value()
        settings.stop_buffer_pct = self.stop_buffer.value() / 100
        settings.trailing_enabled = self.trailing.isChecked()
        settings.trailing_activation_rr = self.trail_activation.value()
        settings.trailing_distance_pct = self.trail_distance.value() / 100
        settings.post_only_entry = self.post_only.isChecked()
        settings.entry_expiry_candles = self.entry_expiry.value()
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
            self.button_forget_key,
            self.take_profit, self.stop_buffer,
            self.trailing, self.trail_activation, self.trail_distance,
            self.risk_unit, self.clamp_size,
            self.post_only, self.entry_expiry,
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

    def _refresh_risk_unit(self) -> None:
        """The suffix and the sensible range both depend on the unit."""
        if self.risk_unit.currentData() == "pct":
            self.risk.setSuffix(" %")
            self.risk.setRange(0.01, 25.0)
        else:
            self.risk.setSuffix(" USDC")
            self.risk.setRange(0.01, 100_000.0)

    def _refresh_trailing_fields(self) -> None:
        on = self.trailing.isChecked()
        self.trail_activation.setEnabled(on)
        self.trail_distance.setEnabled(on)

    def _refresh_entry_fields(self) -> None:
        """Nothing rests when entries cross the spread, so nothing can expire."""
        self.entry_expiry.setEnabled(self.post_only.isChecked())

    def _refresh_strategy_note(self) -> None:
        """Name the strategy that will run, say what it does, grey what it ignores."""
        chosen = self._settings.strategy
        strategy = available().get(chosen)
        self.strategy_name.setText(strategy.display_name if strategy else chosen)
        self.strategy_note.setText(STRATEGY_NOTES.get(chosen, ""))
        # Read off the constructor rather than a hardcoded name, so a strategy
        # added later greys the right fields without anyone remembering to come
        # back and edit this.
        accepted = inspect.signature(strategy.__init__).parameters if strategy else {}
        self.stop_buffer.setEnabled("stop_buffer" in accepted)

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

        # And emptied on the way out, so nothing is left sitting in a box nobody can
        # see. Switching to Paper is abandoning the live setup anyway; the stored key
        # is untouched in Credential Manager either way, since only Save writes it.
        if not live:
            self.agent_key.clear()
        self._refresh_key_controls()

        # And the mirror image: the paper balance does nothing in Live, where the
        # balance is whatever the exchange says. Shown there it reads as a starting
        # stake for real money.
        for widget in (self._balance_label, self.balance):
            widget.setVisible(not live)

        # Margin mode reaches Hyperliquid with the leverage; the paper broker takes
        # it and drops it. Offering the choice where nothing acts on it is the same
        # fault in the other direction.
        for widget in (self._margin_label, self.margin):
            widget.setVisible(live)

        # Reset belongs to the paper broker alone. This one was missed when the
        # fields were audited — the audit walked the form and never looked at the
        # buttons in the header, and it took an AttributeError on a live account to
        # find it.
        self.button_reset.setVisible(not live)

        advisories = self.current().advisories()
        self.timeframe_note.setText(" ".join(advisories))
        self.timeframe_note.setVisible(bool(advisories))
        self.timeframe_note.setStyleSheet(f"color: {theme.AMBER}; background: transparent")

    def _forget_agent_key(self) -> None:
        """Take the stored key back out of Credential Manager, and stop being live.

        Immediate, and not behind a confirmation dialog — a modal would block the
        event loop the bot runs on, which this app avoids everywhere. It is cheaply
        undone by pasting the key again, and Settings are locked while the bot runs,
        so it cannot happen underneath an open position.

        The mode drops to Paper as part of the same action, deliberately. A balance
        is read from the *address*, which needs no key at all, so deleting the key
        alone would leave the dashboard showing a real account this app can no
        longer act on — credentials removed, and everything still reading as live.
        Rewiring to Paper makes the screen true again.
        """
        secrets_store.delete_agent_key()
        self.agent_key.clear()
        self.agent_key.setPlaceholderText("0x...")
        self.mode.setCurrentIndex(self.mode.findData(TradingMode.PAPER))
        self._refresh_notes()  # hides the credential fields, refreshes the buttons

        self.problem.setVisible(False)
        self._settings = self.current()
        self.keyForgotten.emit()
        self.saved.emit(self._settings)

    def _refresh_key_controls(self) -> None:
        """Nothing to forget when nothing is stored."""
        self.button_forget_key.setEnabled(secrets_store.has_agent_key())

    def _emit_saved(self) -> None:
        problems: list[str] = []

        # Only in Live. The key field is hidden in Paper, and text left in it there
        # was still being validated — so a stray character blocked every save with a
        # complaint about a field the user could not see, let alone clear. A hidden
        # control must not be able to refuse anything.
        typed = (
            self.agent_key.text().strip()
            if self.mode.currentData() is TradingMode.LIVE
            else ""
        )
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
