"""Boots the desktop app.

Qt and asyncio share a single event loop through qasync, so the engine's awaits and
the widgets run on the same thread. No locks, no cross-thread signals, and no
background thread that outlives the window.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

# qtpy-based dependencies pick a binding from the environment. finplot drags PyQt6
# in, so pin the choice before any of them import, or two bindings load at once and
# the process dies on the first widget.
os.environ.setdefault("QT_API", "pyside6")

import qasync  # noqa: E402
import qtawesome as qta  # noqa: E402
from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ..config import load_settings  # noqa: E402
from ..db import connect, get_ui_state  # noqa: E402
from ..logging_setup import setup_logging  # noqa: E402
from ..paths import app_dir, log_path  # noqa: E402
from .main_window import MainWindow  # noqa: E402
from .theme import BRAND, STYLESHEET  # noqa: E402

log = logging.getLogger("hypertrade")


def app_icon() -> QIcon:
    """The title bar and taskbar mark: the same bolt the sidebar draws.

    Prefers the packaged `.ico`, which carries every size Windows asks for, and
    falls back to rendering the glyph directly so a source checkout without the
    asset still shows the right thing rather than Qt's default.
    """
    packaged = app_dir() / "assets" / "hypertrade.ico"
    if packaged.is_file():
        return QIcon(str(packaged))
    return qta.icon("fa6s.bolt", color=BRAND)


def run_gui(verbose: bool = False) -> int:
    setup_logging(logging.DEBUG if verbose else logging.INFO)
    log.info("HyperTrade - logging to %s", log_path())

    app = QApplication(sys.argv)
    app.setApplicationName("HyperTrade")
    app.setWindowIcon(app_icon())
    app.setStyleSheet(STYLESHEET)

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    conn = connect()
    try:
        window = MainWindow(conn, load_settings(conn))
        # Maximised by default — the dashboard puts a chart, a log column and five
        # cards side by side, and all of that wants the width. The user's last
        # choice wins on the next launch.
        if get_ui_state(conn, "window_maximized", "1") == "1":
            window.showMaximized()
        else:
            window.show()
        # Queued rather than called: start() schedules coroutines, and there is no
        # running loop until run_forever below. Called here directly, every one of
        # them would be dropped and the app would come up connected to nothing.
        loop.call_soon(window.start)
        with loop:
            loop.run_forever()
    finally:
        conn.close()
    return 0
