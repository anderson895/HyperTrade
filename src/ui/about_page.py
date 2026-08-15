"""About page — what this is, and what to expect from it."""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from .. import __version__

BODY = (
    "BTC/USD perpetual trading bot on Hyperliquid - v{version}\n\n"
    "Strategy: Trend Following (EMA 21/55 crossover, 2xATR stop, 2R target).\n"
    "Data and execution both come from Hyperliquid, so the price the bot\n"
    "reasons about is the price it trades on. No VPN or custom DNS needed.\n\n"
    "Paper mode simulates every trade against real prices and charges the\n"
    "real taker fee and slippage - nothing is free.\n\n"
    "Expect this strategy to lose more trades than it wins. At a 2R target\n"
    "it only needs about 34% of trades to work out to break even; a run of\n"
    "small losses is the system operating normally.\n\n"
    "Live trading is not implemented yet."
)


class AboutPage(QWidget):
    def __init__(self) -> None:
        super().__init__()

        title = QLabel("HyperTrade")
        title.setProperty("h1", True)
        body = QLabel(BODY.format(version=__version__))
        body.setProperty("muted", True)

        root = QVBoxLayout(self)
        root.addWidget(title)
        root.addWidget(body)
        root.addStretch()
