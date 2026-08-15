"""The application window: sidebar, page stack, bottom bar, and the wiring between.

This file coordinates; the pages themselves live next door. It owns three timers —
the one-second UI refresh, a slower one for a custom chart range, and the uptime
counter — and it is the only place that knows a page exists.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3

import qtawesome as qta
from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..config import AppSettings
from ..core.models import Timeframe
from ..db import get_ui_state, set_ui_state
from ..errors import FEED_ERRORS
from ..logging_setup import LogLine, add_ui_sink
from . import theme
from .about_page import AboutPage
from .alert_banner import AlertBanner
from .bottom_bar import BottomBar
from .controller import BotController, Snapshot
from .dashboard_page import DashboardPage
from .logs_page import LogsPage
from .settings_page import SettingsPage
from .stats_page import StatsPage
from .trades_page import TradesPage
from .widgets import WheelBlocker

log = logging.getLogger(__name__)

REFRESH_MS = 1_000
CHART_REFRESH_MS = 20_000
SIDEBAR_WIDE, SIDEBAR_NARROW = 204, 62
NAV_WIDE, NAV_NARROW = 190, 48

PAGE_TRADES, PAGE_STATS = 3, 4


class MainWindow(QMainWindow):
    PAGES = [
        ("fa6s.house", "Dashboard"),
        ("fa6s.gear", "Settings"),
        ("fa6s.file-lines", "Logs"),
        ("fa6s.chart-line", "Trades"),
        ("fa6s.chart-pie", "Statistics"),
        ("fa6s.circle-info", "About"),
    ]

    def __init__(self, conn: sqlite3.Connection, settings: AppSettings) -> None:
        super().__init__()
        self.setWindowTitle("HyperTrade - BTC/USD on Hyperliquid")
        # The bottom bar carries four labelled columns, three buttons and an uptime
        # counter. Below about 1280 they no longer fit and the market label clips to
        # "BTC-USD perp [PA".
        self.setMinimumSize(1280, 720)
        self.setStyleSheet(theme.STYLESHEET)

        self._conn = conn
        self.controller = BotController(conn, settings)
        self._closing = False
        self._snapshot = Snapshot()
        self._uptime_secs = 0

        sidebar = self._build_sidebar()
        self._build_pages(conn, settings)
        self.setCentralWidget(self._build_body(sidebar))

        self._apply_pointer_cursors(self.centralWidget())
        self._connect()
        add_ui_sink(self._on_log)
        self._build_timers()
        self._refresh_config_labels()

    def start(self) -> None:
        """Begin polling and connect to the exchange.

        Kept out of `__init__` so constructing the window needs no event loop and no
        network — which is what makes it testable off-screen.
        """
        self._refresh_timer.start(REFRESH_MS)
        self._chart_timer.start(CHART_REFRESH_MS)
        asyncio.ensure_future(self._start_up())

    # --- construction ----------------------------------------------------

    def _build_sidebar(self) -> QWidget:
        self._brand_icon = QLabel()
        self._brand_icon.setPixmap(qta.icon("fa6s.bolt", color=theme.ACCENT).pixmap(28, 28))
        self._brand = QLabel("HyperTrade")
        self._brand.setProperty("h2", True)

        self._sidebar_btn = QToolButton()
        self._sidebar_btn.setIcon(qta.icon("fa6s.bars", color=theme.MUTED))
        self._sidebar_btn.setIconSize(QSize(18, 18))
        self._sidebar_btn.setToolTip("Toggle sidebar")
        self._sidebar_btn.clicked.connect(self._toggle_sidebar)

        brand_row = QHBoxLayout()
        brand_row.setSpacing(8)
        brand_row.addWidget(self._brand_icon)
        brand_row.addWidget(self._brand, stretch=1)
        brand_row.addWidget(self._sidebar_btn)

        self._brand_sub = QLabel("BTC/USD perpetual")
        self._brand_sub.setProperty("muted", True)

        self._nav = QListWidget()
        self._nav.setObjectName("sidebar")
        self._nav.setIconSize(QSize(18, 18))
        self._nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for icon_name, label in self.PAGES:
            icon = qta.icon(icon_name, color=theme.MUTED, color_selected=theme.ACCENT_TEXT)
            item = QListWidgetItem(icon, label)
            item.setToolTip(label)
            self._nav.addItem(item)
        self._nav.setCurrentRow(0)
        self._nav.setFixedWidth(NAV_WIDE)

        self._version = QLabel(f"v{__version__}")
        self._version.setProperty("muted", True)

        self._sidebar = QWidget()
        column = QVBoxLayout(self._sidebar)
        column.setContentsMargins(14, 14, 0, 14)
        column.addLayout(brand_row)
        column.addWidget(self._brand_sub)
        column.addSpacing(14)
        column.addWidget(self._nav, stretch=1)
        column.addWidget(self._version)
        self._sidebar.setFixedWidth(SIDEBAR_WIDE)

        self._sidebar_collapsed = get_ui_state(self._conn, "sidebar_collapsed", "0") == "1"
        if self._sidebar_collapsed:
            self._apply_sidebar(True)
        return self._sidebar

    def _build_pages(self, conn: sqlite3.Connection, settings: AppSettings) -> None:
        self.dash = DashboardPage(conn)
        self.settings_page = SettingsPage(settings)
        self.logs = LogsPage()
        self.trades = TradesPage(conn)
        self.stats = StatsPage(conn)

        self._stack = QStackedWidget()
        for page in (self.dash, self.settings_page, self.logs, self.trades,
                     self.stats, AboutPage()):
            self._stack.addWidget(page)

    def _build_body(self, sidebar: QWidget) -> QWidget:
        self.bottom = BottomBar()
        self.alert = AlertBanner()

        content = QVBoxLayout()
        content.setContentsMargins(10, 14, 14, 14)
        content.addWidget(self.alert)
        content.addWidget(self._stack, stretch=1)
        content.addWidget(self.bottom)

        root = QHBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(sidebar)
        root.addLayout(content, stretch=1)

        container = QWidget()
        container.setLayout(root)
        return container

    def _build_timers(self) -> None:
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(
            lambda: asyncio.ensure_future(self.controller.refresh())
        )
        # A custom chart range is not fed by the engine's buffer, so it needs its own
        # refresh - slower, because a week of hourly candles does not change often.
        self._chart_timer = QTimer(self)
        self._chart_timer.timeout.connect(self._refresh_chart_range)

        self._uptime_timer = QTimer(self)
        self._uptime_timer.setInterval(1_000)
        self._uptime_timer.timeout.connect(self._tick_uptime)

    def _connect(self) -> None:
        self.controller.updated.connect(self._on_update)
        self.controller.failed.connect(self.alert.show_error)
        self.controller.settings_applied.connect(self._on_settings_applied)

        self._nav.currentRowChanged.connect(self._on_page_changed)
        self.bottom.start_btn.clicked.connect(
            lambda: asyncio.ensure_future(self.controller.start())
        )
        self.bottom.stop_btn.clicked.connect(
            lambda: asyncio.ensure_future(self.controller.stop())
        )
        self.bottom.close_btn.clicked.connect(
            lambda: asyncio.ensure_future(self.controller.close_position())
        )
        self.settings_page.saved.connect(
            lambda settings: asyncio.ensure_future(self.controller.apply_settings(settings))
        )
        self.settings_page.resetPaper.connect(
            lambda: asyncio.ensure_future(self.controller.reset_paper())
        )
        self.dash.chartRangeRequested.connect(
            lambda timeframe, count: asyncio.ensure_future(
                self._load_chart_range(timeframe, count)
            )
        )

    def _apply_pointer_cursors(self, container: QWidget) -> None:
        """Hand cursor on everything clickable, and no wheel on value inputs.

        Scrolling the page with the cursor over Risk or Leverage would otherwise
        change them without the user noticing.
        """
        for cls in (QPushButton, QToolButton, QComboBox, QCheckBox):
            for widget in container.findChildren(cls):
                widget.setCursor(Qt.CursorShape.PointingHandCursor)
        self._nav.viewport().setCursor(Qt.CursorShape.PointingHandCursor)

        self._wheel_blocker = WheelBlocker(self)
        for cls in (QDoubleSpinBox, QSpinBox, QComboBox):
            for widget in container.findChildren(cls):
                widget.installEventFilter(self._wheel_blocker)
                widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    async def _start_up(self) -> None:
        if await self.controller.initialise():
            self.settings_page.set_max_leverage(self.controller.asset.max_leverage)
        await self.controller.refresh()
        # A restored non-default range asked for its candles while DashboardPage was
        # still being built, before this window had connected to the signal, so that
        # request went nowhere. Ask again now that everything is wired — otherwise
        # the chart sits empty until the 20-second refresh eventually fires.
        self._refresh_chart_range()
        self.trades.reload(self._snapshot.mode)
        self.stats.refresh(self._snapshot.mode)

    # --- sidebar ---------------------------------------------------------

    def _toggle_sidebar(self) -> None:
        self._sidebar_collapsed = not self._sidebar_collapsed
        self._apply_sidebar(self._sidebar_collapsed)
        set_ui_state(self._conn, "sidebar_collapsed", "1" if self._sidebar_collapsed else "0")

    def _apply_sidebar(self, collapsed: bool) -> None:
        """Collapsed is icon-only with tooltips; the body takes the freed width."""
        for widget in (self._brand_icon, self._brand, self._brand_sub, self._version):
            widget.setVisible(not collapsed)
        for index, (_icon, label) in enumerate(self.PAGES):
            self._nav.item(index).setText("" if collapsed else label)
        self._nav.setFixedWidth(NAV_NARROW if collapsed else NAV_WIDE)
        self._sidebar.setFixedWidth(SIDEBAR_NARROW if collapsed else SIDEBAR_WIDE)
        self._nav.setProperty("collapsed", collapsed)
        self._nav.style().unpolish(self._nav)
        self._nav.style().polish(self._nav)

    # --- chart -----------------------------------------------------------

    async def _load_chart_range(self, timeframe: Timeframe, count: int) -> None:
        """Fetch a chart-only view at a different interval from the bot's."""
        try:
            candles = await self.controller.fetch_chart_candles(timeframe, count)
        except FEED_ERRORS as exc:
            log.warning("could not load the %s chart view: %s", timeframe.value, exc)
            return
        if len(candles) < 2:
            return
        # The last is still forming, so it is handed over separately and the live
        # price keeps stretching it between fetches.
        self.dash.load_candles(candles[:-1], candles[-1])

    def _refresh_chart_range(self) -> None:
        """Keep a custom range current; the bot view refreshes with the engine."""
        request = self.dash.current_request()
        if request is not None:
            asyncio.ensure_future(self._load_chart_range(*request))

    # --- slots -----------------------------------------------------------

    def _on_page_changed(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        if index == PAGE_TRADES:
            self.trades.reload(self._snapshot.mode)
        elif index == PAGE_STATS:
            self.stats.refresh(self._snapshot.mode)

    def _on_update(self, snapshot: Snapshot) -> None:
        was_holding = self._snapshot.position is not None
        was_running = self._snapshot.running
        self._snapshot = snapshot

        # The chart's own candles come from `_load_chart_range`; this only moves the
        # live price, which redraws the forming candle's tip.
        self.dash.apply(snapshot)

        self.bottom.start_btn.setEnabled(snapshot.ready and not snapshot.running)
        self.bottom.stop_btn.setEnabled(snapshot.running)
        self.bottom.close_btn.setEnabled(snapshot.position is not None)
        # Locked while running: changing the timeframe mid-trade would leave the
        # strategy reasoning about candles it never saw.
        self.settings_page.set_enabled(not snapshot.running)

        if snapshot.running and not was_running:
            self._uptime_secs = 0
            self._uptime_timer.start()
            self.dash.set_strategy_status("running - waiting for a candle to close")
        elif not snapshot.running and was_running:
            self._uptime_timer.stop()
            self.dash.set_strategy_status("stopped")

        if was_holding != (snapshot.position is not None):
            self.trades.reload(snapshot.mode)
            self.stats.refresh(snapshot.mode)

        if snapshot.error:
            self.alert.show_error(snapshot.error)

    def _on_settings_applied(self, settings: AppSettings) -> None:
        self.settings_page.load(settings)
        self._refresh_config_labels()

    def _refresh_config_labels(self) -> None:
        settings = self.controller.settings
        self.dash.set_strategy_context(
            settings.timeframe, self.controller.strategy_parameters()
        )
        self.bottom.show_config(
            market=f"BTC-USD perp [{settings.trading_mode.value.upper()}]",
            timeframe=settings.timeframe.label,
            risk=f"{settings.risk_usdc:,.2f} USDC",
            leverage=f"{settings.leverage}x",
        )

    def _on_log(self, line: LogLine) -> None:
        self.logs.add_log(line)
        self.dash.add_log(line.message, line.levelno)
        if line.levelno >= logging.ERROR:
            self.alert.show_error(line.message)

    def _tick_uptime(self) -> None:
        self._uptime_secs += 1
        self.bottom.show_uptime(self._uptime_secs)

    # --- shutdown --------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802 — Qt naming
        if self._closing:
            event.accept()
            return
        self._closing = True
        self._refresh_timer.stop()
        self._chart_timer.stop()
        self._uptime_timer.stop()
        set_ui_state(self._conn, "window_maximized", "1" if self.isMaximized() else "0")

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop, so there is nothing async to unwind: the window was
            # built without one, which happens in tests and if startup failed early.
            # Checked before building the coroutine, so none is left un-awaited.
            event.accept()
            return

        loop.create_task(self._shutdown())
        event.ignore()

    async def _shutdown(self) -> None:
        await self.controller.shutdown()
        QApplication.instance().quit()
