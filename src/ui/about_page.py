"""About page — what this is, what to expect from it, and what it would trade now.

The sizing read-out lives here rather than on Settings. It is reference material:
you consult it once to understand what a risk and leverage pair actually produces,
not on every visit to change a field.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

from .. import __version__
from . import theme
from .chrome import PageHeader
from .controller import Preview
from .widgets import TitledCard, note_box, note_label

STRATEGY = (
    "Trend following on candle close, with an ATR-based stop and a target at 2R. "
    "A position is opened only when the settings can size one."
)
DATA = (
    "Data and execution both come from Hyperliquid, so the price the bot reasons "
    "about is the price it trades on. No VPN or custom DNS needed."
)
PAPER = (
    "Paper mode simulates every trade against real prices and charges the real taker "
    "fee and slippage - nothing is free. Live mode signs orders with an approved API "
    "wallet, which can trade but cannot withdraw."
)
EXPECTATIONS = (
    "Expect this strategy to lose more trades than it wins. At a 2R target it only "
    "needs about 34% of trades to work out to break even, so a run of small losses "
    "is the system operating normally, not a fault."
)


class _PreviewCard(TitledCard):
    """What the settings on the Settings page would do, at the prices in front of us."""

    ROWS = (
        ("price", "BTC now"),
        ("equity", "Account equity"),
        ("stop", "Stop distance"),
        ("size", "Position size"),
        ("notional", "Notional"),
        ("margin", "Margin needed"),
    )

    def __init__(self) -> None:
        super().__init__("fa6s.circle-info", "What this does")

        self.add(
            note_label("A trade sized from your current settings, right now.")
        )
        self._column.addSpacing(10)

        self._values: dict[str, QLabel] = {}
        for key, label in self.ROWS:
            caption = QLabel(label)
            caption.setProperty("muted", True)
            value = QLabel("-")
            value.setStyleSheet("font-weight: bold; background: transparent")
            value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._values[key] = value

            row = QHBoxLayout()
            row.setContentsMargins(0, 3, 0, 3)
            row.addWidget(caption)
            row.addStretch()
            row.addWidget(value)
            self._column.addLayout(row)

        self._column.addSpacing(12)
        self.verdict = note_label("")
        self.add(self.verdict)

        self._column.addSpacing(12)
        self.add(
            note_box(
                "Note",
                "Use paper trading first to test your settings before going live.",
            )
        )
        self.finish()
        # Drawn as if waiting from the start, so it never looks broken while the
        # first prices are still arriving.
        self.show_preview(Preview())

    def show_preview(self, preview: Preview) -> None:
        if not preview.ready:
            for value in self._values.values():
                value.setText("-")
            self.verdict.setText("Waiting for prices...")
            self.verdict.setStyleSheet(f"color: {theme.MUTED}; background: transparent")
            return

        self._values["price"].setText(f"{preview.price:,.1f}")
        self._values["equity"].setText(f"{preview.equity:,.2f} USDC")
        self._values["stop"].setText(f"{preview.stop_distance:,.0f}")

        if preview.problem:
            for key in ("size", "notional", "margin"):
                self._values[key].setText("-")
            hint = f" - {preview.hint}" if preview.hint else ""
            self.verdict.setText(f"Cannot trade: {preview.problem}{hint}")
            self.verdict.setStyleSheet(f"color: {theme.RED}; background: transparent")
            return

        self._values["size"].setText(f"{preview.size:g} BTC")
        self._values["notional"].setText(f"{preview.notional:,.0f} USDC")
        self._values["margin"].setText(f"{preview.margin:,.0f} USDC")
        self.verdict.setText("These settings can trade.")
        self.verdict.setStyleSheet(f"color: {theme.ACCENT}; background: transparent")


class AboutPage(QWidget):
    def __init__(self) -> None:
        super().__init__()

        self.preview_card = _PreviewCard()

        left = QVBoxLayout()
        left.setSpacing(16)
        left.addWidget(self.preview_card)
        left.addStretch()

        right = QVBoxLayout()
        right.setSpacing(16)
        right.addWidget(self._build_about_card())
        right.addStretch()

        cards = QHBoxLayout()
        cards.setSpacing(16)
        cards.addLayout(left, 1)
        cards.addLayout(right, 1)

        inner = QWidget()
        column = QVBoxLayout(inner)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(18)
        column.addWidget(
            PageHeader(
                "fa6s.circle-info",
                "About HyperTrade",
                f"Version {__version__} - BTC/USD perpetual on Hyperliquid",
            )
        )
        column.addLayout(cards)
        column.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(scroll)

    def _build_about_card(self) -> TitledCard:
        card = TitledCard("fa6s.book-open", "How it works")
        for heading, body in (
            ("Strategy", STRATEGY),
            ("Data and execution", DATA),
            ("Paper and live", PAPER),
            ("What to expect", EXPECTATIONS),
        ):
            caption = QLabel(heading)
            caption.setStyleSheet(f"color: {theme.ACCENT}; font-weight: bold")
            caption.setContentsMargins(0, 10, 0, 0)
            card.add(caption)
            card.note(body)
        card.finish()
        return card

    def show_preview(self, preview: Preview) -> None:
        self.preview_card.show_preview(preview)
