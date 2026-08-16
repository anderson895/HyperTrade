"""Alert banner — a loud but non-blocking notice at the top of the window.

Deliberately not a modal dialog: a dialog would freeze the bot loop behind a button
nobody is there to press. It is dismissible, and counts errors when they pile up.

It carries good news as well as bad. A save that worked used to look identical to a
save that was never clicked — the busy scrim came and went and nothing else changed.
"""

from __future__ import annotations

import qtawesome as qta
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QToolButton

from . import theme

#: How long a confirmation stays up. Long enough to read a line, short enough that
#: it is gone before it turns into furniture nobody sees any more.
SUCCESS_MS = 5_000


class AlertBanner(QFrame):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("alertBanner")

        self._icon = QLabel()
        self._label = QLabel("")
        self._label.setWordWrap(True)

        self._close = QToolButton()
        self._close.setStyleSheet("border: none; background: transparent")
        self._close.clicked.connect(self.dismiss)

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 8, 8)
        row.addWidget(self._icon)
        row.addWidget(self._label, stretch=1)
        row.addWidget(self._close)

        # Errors are dismissed by hand; only confirmations time out.
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)

        self._count = 0
        self.hide()

    def _paint(self, background: str, border: str, foreground: str, icon: str) -> None:
        self.setStyleSheet(
            f"""
            QFrame#alertBanner {{
                background: {background};
                border: 1px solid {border};
                border-radius: 8px;
            }}
            QFrame#alertBanner QLabel {{
                color: {foreground};
                font-weight: bold;
            }}
            """
        )
        self._icon.setPixmap(qta.icon(icon, color=foreground).pixmap(18, 18))
        self._close.setIcon(qta.icon("fa6s.xmark", color=foreground))

    def show_error(self, message: str) -> None:
        self._timer.stop()  # an error outranks a confirmation still counting down
        self._count += 1
        prefix = f"({self._count} errors) " if self._count > 1 else ""
        self._paint("#4a1518", theme.RED, "#fecaca", "fa6s.triangle-exclamation")
        self._label.setText(f"{prefix}{message}")
        self.show()

    def show_success(self, message: str) -> None:
        """Confirm something worked, then get out of the way.

        Never paints over an error. Applying settings in Live mode can fall back to
        Paper, and that failure is raised before the "applied" signal that brings us
        here — a green "Saved" on top of it would be the app burying its own bad
        news at the exact moment the user most needs to see it.
        """
        if self._count:
            return
        self._paint(theme.ACCENT_DIM, theme.GREEN, theme.ACCENT_TEXT, "fa6s.circle-check")
        self._label.setText(message)
        self.show()
        self._timer.start(SUCCESS_MS)

    def dismiss(self) -> None:
        self._timer.stop()
        self._count = 0
        self.hide()
