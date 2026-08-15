"""Alert banner — a loud but non-blocking error notice at the top of the window.

Deliberately not a modal dialog: a dialog would freeze the bot loop behind a button
nobody is there to press. It is dismissible, and counts errors when they pile up.
"""

from __future__ import annotations

import qtawesome as qta
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QToolButton

from . import theme


class AlertBanner(QFrame):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("alertBanner")
        self.setStyleSheet(
            f"""
            QFrame#alertBanner {{
                background: #4a1518;
                border: 1px solid {theme.RED};
                border-radius: 8px;
            }}
            QFrame#alertBanner QLabel {{
                color: #fecaca;
                font-weight: bold;
            }}
            """
        )

        icon = QLabel()
        icon.setPixmap(qta.icon("fa6s.triangle-exclamation", color="#fecaca").pixmap(18, 18))
        self._label = QLabel("")
        self._label.setWordWrap(True)

        close = QToolButton()
        close.setIcon(qta.icon("fa6s.xmark", color="#fecaca"))
        close.setStyleSheet("border: none; background: transparent")
        close.clicked.connect(self.dismiss)

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 8, 8)
        row.addWidget(icon)
        row.addWidget(self._label, stretch=1)
        row.addWidget(close)

        self._count = 0
        self.hide()

    def show_error(self, message: str) -> None:
        self._count += 1
        prefix = f"({self._count} errors) " if self._count > 1 else ""
        self._label.setText(f"{prefix}{message}")
        self.show()

    def dismiss(self) -> None:
        self._count = 0
        self.hide()
