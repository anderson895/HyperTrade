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
from PySide6.QtWidgets import QApplication  # noqa: E402

from ..config import load_settings  # noqa: E402
from ..db import connect, get_ui_state  # noqa: E402
from ..logging_setup import setup_logging  # noqa: E402
from ..paths import log_path  # noqa: E402
from .main_window import MainWindow  # noqa: E402
from .theme import STYLESHEET  # noqa: E402

log = logging.getLogger("hypertrade")


def run_gui(verbose: bool = False) -> int:
    setup_logging(logging.DEBUG if verbose else logging.INFO)
    log.info("HyperTrade - logging to %s", log_path())

    app = QApplication(sys.argv)
    app.setApplicationName("HyperTrade")
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
        window.start()
        with loop:
            loop.run_forever()
    finally:
        conn.close()
    return 0
