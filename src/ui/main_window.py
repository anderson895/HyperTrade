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
from .busy_overlay import BusyOverlay
from .chrome import PageHeader, StatusBar, TopBar, divider
from .controller import BotController, Snapshot
from .dashboard_page import DashboardPage
from .logs_page import LogsPage
from ..strategy import available
from .settings_page import SettingsPage
from .stats_page import StatsPage
from .trades_page import TradesPage
from .widgets import Card, WheelBlocker

log = logging.getLogger(__name__)

REFRESH_MS = 1_000
CHART_REFRESH_MS = 20_000
# Wide enough for the 34px bolt plus the 23px wordmark on one line; below this the
# wordmark elides to "HyperTra...".
SIDEBAR_WIDE, SIDEBAR_NARROW = 222, 62
NAV_WIDE, NAV_NARROW = 208, 48

PAGE_SETTINGS, PAGE_TRADES, PAGE_STATS, PAGE_ABOUT = 1, 3, 4, 5


class MainWindow(QMainWindow):
    #: nav icon, sidebar label, top-bar icon, top-bar title, breadcrumb.
    #: The top bar names the page rather than the nav item — "Bot Settings", not
    #: "Settings" — and carries its own icon, as in the reference design.
    PAGES = [
        ("fa6s.house", "Dashboard", "fa6s.gauge-high", "Live Dashboard", "Market Overview"),
        ("fa6s.gear", "Settings", "fa6s.robot", "Bot Settings", "Account Configuration"),
        ("fa6s.file-lines", "Logs", "fa6s.file-lines", "Event Log", "Recent Activity"),
        ("fa6s.chart-line", "Trades", "fa6s.chart-line", "Trade History", "Fills and Results"),
        ("fa6s.chart-pie", "Statistics", "fa6s.chart-pie", "Statistics", "Performance"),
        ("fa6s.circle-info", "About", "fa6s.circle-info", "About HyperTrade", "Version and Strategy"),
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

        # Parented to the central widget so it covers the pages and the bottom bar,
        # which is the point — START must not be reachable mid-rewire.
        self.busy = BusyOverlay(self.centralWidget())

        self._apply_pointer_cursors(self.centralWidget())
        self._connect()
        add_ui_sink(self._on_log)
        self._build_timers()
        self._refresh_config_labels()
        # Set by hand: the nav starts on row 0, so selecting it fires no signal and
        # the breadcrumb would read "Dashboard /" with nothing after the slash.
        self.top.set_page(*self.PAGES[0][2:])

    def start(self) -> None:
        """Begin polling and connect to the exchange.

        Kept out of `__init__` so constructing the window needs no event loop and no
        network — which is what makes it testable off-screen.
        """
        self._refresh_timer.start(REFRESH_MS)
        self._chart_timer.start(CHART_REFRESH_MS)
        self._schedule(self._start_up)

    def _schedule(self, coroutine, *args) -> None:
        """Run a coroutine on the shared loop, or drop it if there is none.

        The window is deliberately constructible without a loop — that is what makes
        it testable off-screen — and some of these are triggered by rendering rather
        than by a click. Checked before the coroutine is built, so none is left
        un-awaited.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(coroutine(*args))
        task.add_done_callback(self._report_failure)

    @staticmethod
    def _report_failure(task: asyncio.Task) -> None:
        """Surface a crash inside a fire-and-forget task.

        Without this, an exception during start-up vanishes into the task and the
        window simply comes up connected to nothing, with an empty log and no clue.
        """
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return

        # KeyboardInterrupt and SystemExit are not Exceptions, and they are not
        # failures either — they are someone asking the app to stop. `task.exception()`
        # hands them over all the same. Logged and swallowed, Ctrl+C left the loop
        # running, the window open and the process to be killed by hand; `run_gui`'s
        # `finally: conn.close()` never ran, the SQLite WAL was orphaned, and every
        # saved setting went with it.
        if not isinstance(error, Exception):
            log.info("shutting down on %s", type(error).__name__)
            QApplication.instance().quit()
            return

        log.error("background task failed: %s", error, exc_info=error)

    # --- construction ----------------------------------------------------

    def _build_sidebar(self) -> QWidget:
        self._brand_icon = QLabel()
        self._brand_icon.setPixmap(qta.icon("fa6s.bolt", color=theme.BRAND).pixmap(34, 34))
        self._brand = QLabel("HyperTrade")
        self._brand.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 23px; font-weight: bold; font-style: italic"
        )

        brand_row = QHBoxLayout()
        brand_row.setSpacing(8)
        brand_row.addWidget(self._brand_icon)
        brand_row.addWidget(self._brand, stretch=1)

        self._brand_sub = QLabel("HYPERLIQUID TRADING BOT")
        self._brand_sub.setStyleSheet(f"color: {theme.MUTED}; font-size: 9px; letter-spacing: 1px")
        # Centred, so it sits under the wordmark rather than under the bolt.
        self._brand_sub.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        self._nav = QListWidget()
        self._nav.setObjectName("sidebar")
        self._nav.setIconSize(QSize(18, 18))
        self._nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for icon_name, label, *_rest in self.PAGES:
            icon = qta.icon(icon_name, color=theme.MUTED, color_selected=theme.ACCENT_TEXT)
            item = QListWidgetItem(icon, label)
            item.setToolTip(label)
            self._nav.addItem(item)
        self._nav.setCurrentRow(0)
        self._nav.setFixedWidth(NAV_WIDE)

        self._version = QLabel(f"v{__version__}")
        self._version.setProperty("muted", True)

        self._promo = Card()
        promo_column = QVBoxLayout(self._promo)
        promo_column.setContentsMargins(12, 10, 12, 10)
        promo_column.setSpacing(2)
        tagline = QLabel("Trade Smarter.\nAutomate Better.")
        tagline.setStyleSheet(f"color: {theme.ACCENT}; font-weight: bold; background: transparent")
        product = QLabel("HyperTrade Bot")
        product.setStyleSheet(f"color: {theme.MUTED}; background: transparent")
        promo_column.addWidget(tagline)
        promo_column.addWidget(product)

        self._sidebar = QWidget()
        column = QVBoxLayout(self._sidebar)
        column.setContentsMargins(14, 14, 10, 14)
        column.addLayout(brand_row)
        column.addWidget(self._brand_sub)
        column.addSpacing(14)
        column.addWidget(self._nav, stretch=1)
        column.addWidget(self._promo)
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

        self.about = AboutPage()

        self._stack = QStackedWidget()
        for page in (self.dash, self.settings_page, self.logs, self.trades,
                     self.stats, self.about):
            self._stack.addWidget(page)

    def _build_body(self, sidebar: QWidget) -> QWidget:
        self.bottom = BottomBar()
        self.alert = AlertBanner()
        self.top = TopBar()
        self.status = StatusBar()
        self.top.toggle_btn.clicked.connect(self._toggle_sidebar)

        inner = QVBoxLayout()
        inner.setContentsMargins(14, 12, 14, 12)
        inner.setSpacing(10)
        inner.addWidget(self.alert)
        inner.addWidget(self._stack, stretch=1)
        inner.addWidget(self.bottom)

        content = QVBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(0)
        content.addWidget(self.top)
        content.addLayout(inner, stretch=1)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.addWidget(sidebar)
        # The sidebar and the top bar share a background, so without this rule the
        # seam between them disappears and the wordmark floats in the top bar.
        body.addWidget(divider())
        body.addLayout(content, stretch=1)

        # The status strip runs the full width, under the sidebar too.
        root = QVBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addLayout(body, stretch=1)
        root.addWidget(self.status)

        container = QWidget()
        container.setLayout(root)
        return container

    def _build_timers(self) -> None:
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(
            lambda: self._schedule(self.controller.refresh)
        )
        # A custom chart range is not fed by the engine's buffer, so it needs its own
        # refresh - slower, because a week of hourly candles does not change often.
        self._chart_timer = QTimer(self)
        self._chart_timer.timeout.connect(self._refresh_chart_range)

        self._uptime_timer = QTimer(self)
        self._uptime_timer.setInterval(1_000)
        self._uptime_timer.timeout.connect(self._tick_uptime)

        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(1_000)
        self._clock_timer.timeout.connect(self.status.tick_clock)
        self._clock_timer.start()
        self.status.tick_clock()

    def _connect(self) -> None:
        self.controller.updated.connect(self._on_update)
        self.controller.failed.connect(self._on_failed)
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
        self.settings_page.saved.connect(self._on_settings_saved)
        self.settings_page.resetPaper.connect(
            lambda: asyncio.ensure_future(self.controller.reset_paper())
        )
        self.settings_page.previewRequested.connect(
            lambda: self._schedule(self._refresh_preview)
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
        for widget in (self._brand_icon, self._brand, self._brand_sub,
                       self._version, self._promo):
            widget.setVisible(not collapsed)
        for index, (_icon, label, *_rest) in enumerate(self.PAGES):
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
        # price keeps stretching it between fetches. The timeframe rides along so the
        # percentage beside the price can name the close it measured against.
        self.dash.load_candles(candles[:-1], candles[-1], timeframe)

    async def _refresh_preview(self) -> None:
        """Re-size a hypothetical trade under whatever is on the Settings form.

        Sized from Settings, shown on About: the form is where the numbers are typed,
        the About card is where you read what they would actually produce.
        """
        try:
            preview = await self.controller.preview(self.settings_page.current())
        except FEED_ERRORS as exc:
            log.debug("could not build the settings preview: %s", exc)
            return
        self.about.show_preview(preview)

    def _refresh_chart_range(self) -> None:
        """Keep a custom range current; the bot view refreshes with the engine."""
        request = self.dash.current_request()
        if request is not None:
            self._schedule(self._load_chart_range, *request)

    # --- slots -----------------------------------------------------------

    def _on_page_changed(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        _nav_icon, _label, icon, title, crumb = self.PAGES[index]
        self.top.set_page(icon, title, crumb)
        if index == PAGE_ABOUT:
            self._schedule(self._refresh_preview)
        elif index == PAGE_TRADES:
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

        self.top.connection.set_state(
            snapshot.connected,
            "Connected to Hyperliquid" if snapshot.connected else "Disconnected",
        )
        self.status.set_state(snapshot.running, snapshot.ready)
        self.status.set_network(
            self.controller.settings.network.value.capitalize(),
            snapshot.mode.value.upper(),
        )

        if self._stack.currentIndex() == PAGE_ABOUT:
            self._schedule(self._refresh_preview)

        if snapshot.error:
            self.alert.show_error(snapshot.error)

    def _on_settings_saved(self, settings: AppSettings) -> None:
        """Saving is not instant, so say so while it happens.

        In Live mode this rebuilds the broker: a new `Exchange` is constructed,
        which fetches the whole asset universe, and the account is read back. For a
        few seconds the form looked untouched — no error, no change, no way to tell
        the click had registered.
        """
        detail = (
            "Connecting to Hyperliquid and checking the account"
            if settings.is_live
            else "Rebuilding the simulated account"
        )
        self.busy.start("Applying settings...", detail)
        asyncio.ensure_future(self.controller.apply_settings(settings))

    def _on_settings_applied(self, settings: AppSettings) -> None:
        self.busy.stop()
        self.settings_page.load(settings)
        self._refresh_config_labels()

    def _on_failed(self, message: str) -> None:
        """A failure ends whatever was being waited on, so the scrim goes too."""
        self.busy.stop()
        self.alert.show_error(message)

    def _refresh_config_labels(self) -> None:
        settings = self.controller.settings
        # A percentage has no fixed USDC value, so it is shown as what it is.
        # Printing `risk_usdc` regardless had the bar reading "0.10 USDC" while the
        # engine staked 0.30% of equity — the number on screen was not the number
        # being risked.
        #
        # "of equity" is left off: the column header already says "Risk / Trade",
        # under which a bare percentage has only one reading, and those four
        # characters are the width the fifth column needed. The unit is still
        # unmistakable — the alternative reads "12.50 USDC". Settings and the logs
        # both spell it out in full.
        risk = (
            f"{settings.risk_pct:.2%}" if settings.risk_pct
            else f"{settings.risk_usdc:,.2f} USDC"
        )
        strategy = available().get(settings.strategy)
        self.bottom.show_config(
            # "perp" dropped: the fifth column needed the width, and the status bar
            # already carries the mode. This is the only place it is abbreviated.
            market=f"BTC-USD [{settings.trading_mode.value.upper()}]",
            strategy=strategy.display_name.split(" (")[0] if strategy else settings.strategy,
            timeframe=settings.timeframe.label,
            risk=risk,
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
        self._clock_timer.stop()
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
