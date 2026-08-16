"""The bar along the bottom: what is configured, and the run controls."""

from __future__ import annotations

import qtawesome as qta
from PySide6.QtWidgets import QFrame, QHBoxLayout, QPushButton

from . import theme
from .widgets import labelled_column


class BottomBar(QFrame):
    def __init__(self) -> None:
        super().__init__()
        self.setProperty("card", True)

        # The strategy is named here because a live account was once started on the
        # wrong one and this bar did not say which was loaded. One strategy ships
        # today, but the registry behind the Settings dropdown takes more, and the
        # cost of naming it is one column.
        market_col, self.market_label = labelled_column("Market", "BTC-USD [PAPER]")
        strategy_col, self.strategy_label = labelled_column("Strategy", "-")
        timeframe_col, self.timeframe_label = labelled_column("Timeframe", "-")
        risk_col, self.risk_label = labelled_column("Risk / Trade", "-")
        leverage_col, self.leverage_label = labelled_column("Leverage", "-")
        uptime_col, self.uptime_label = labelled_column("Uptime", "00:00:00")
        self.uptime_label.setStyleSheet("font-weight: bold; font-size: 15px")

        self.start_btn = QPushButton("  START BOT")
        self.start_btn.setObjectName("startBtn")
        self.start_btn.setIcon(qta.icon("fa6s.play", color="white"))

        self.stop_btn = QPushButton("  STOP BOT")
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.setIcon(qta.icon("fa6s.stop", color=theme.RED))
        self.stop_btn.setEnabled(False)

        self.close_btn = QPushButton("  Close position")
        self.close_btn.setIcon(qta.icon("fa6s.xmark", color=theme.MUTED))
        self.close_btn.setEnabled(False)
        self.close_btn.setToolTip(
            "Flatten now. STOP BOT stops looking for entries but leaves an open "
            "position alone with its stop and target."
        )

        # Without minimums the layout shrinks them below their text and the labels
        # clip to "START B(".
        self.start_btn.setMinimumWidth(140)
        self.stop_btn.setMinimumWidth(130)
        self.close_btn.setMinimumWidth(150)

        row = QHBoxLayout(self)
        row.setContentsMargins(16, 10, 16, 10)
        # Tighter than the original four columns, so the fifth fits without
        # squeezing the buttons into "START B(". There is a test that measures this
        # at the smallest allowed window; it caught the first attempt.
        #
        # Spacing goes *between* columns, not after the last one — a stretch follows
        # it, so that trailing gap was width spent on nothing.
        columns = (market_col, strategy_col, timeframe_col, risk_col, leverage_col)
        for index, column in enumerate(columns):
            if index:
                row.addSpacing(8)
            row.addLayout(column)
        row.addStretch()
        row.addWidget(self.start_btn)
        row.addSpacing(8)
        row.addWidget(self.stop_btn)
        row.addSpacing(8)
        row.addWidget(self.close_btn)
        row.addStretch()
        row.addSpacing(12)
        row.addLayout(uptime_col)

    def show_config(
        self, market: str, timeframe: str, risk: str, leverage: str, strategy: str = "-"
    ) -> None:
        self.market_label.setText(market)
        self.strategy_label.setText(strategy)
        self.timeframe_label.setText(timeframe)
        self.risk_label.setText(risk)
        self.leverage_label.setText(leverage)

    def show_uptime(self, seconds: int) -> None:
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        self.uptime_label.setText(f"{hours:02d}:{minutes:02d}:{secs:02d}")
