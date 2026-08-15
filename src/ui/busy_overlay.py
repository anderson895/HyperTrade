"""A "working on it" scrim for actions that are not instant.

Saving settings in Live mode rebuilds the broker: a new `Exchange` is constructed,
which fetches the whole asset universe, and the account is read back from
Hyperliquid. That is a few seconds during which the form looked exactly as it had
before the click — no error, no change, no way to tell whether the button had done
anything. The honest answer is "it is working", so this says so.

Translucent rather than opaque, unlike a startup screen: the action is brief and the
form underneath is still the thing being changed. Blanking it would suggest the app
had gone somewhere else.
"""

from __future__ import annotations

import qtawesome as qta
from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from . import theme

#: Nothing here should take this long. If it does, the overlay lets go anyway — a
#: user trapped behind a scrim is worse off than one looking at a stale form, and
#: whatever went wrong will have been logged and raised as a banner.
TIMEOUT_MS = 20_000

SPIN_INTERVAL_MS = 70


class BusyOverlay(QWidget):
    """Covers its parent with a dimmed scrim, a spinner and one line of text."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.hide()
        #: Whether an action is in flight. Kept as a flag rather than read back from
        #: `isVisible()`, which is False whenever an ancestor is hidden — true of
        #: every window that has not been shown yet, and of the whole app while
        #: minimised. Being busy is a fact about the app, not about pixels.
        self._active = False

        # The overlay keeps itself the size of its parent. It cannot be set once in
        # __init__ — the parent has not been laid out yet at that point — and not
        # every resize passes through whoever constructed it.
        if parent is not None:
            parent.installEventFilter(self)
            self.setGeometry(parent.rect())

        self._spinner = QLabel()
        self._spinner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._angle = 0

        self._message = QLabel("")
        self._message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 15px; font-weight: bold; background: transparent"
        )

        self._detail = QLabel("")
        self._detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._detail.setStyleSheet(
            f"color: {theme.MUTED}; font-size: 13px; background: transparent"
        )

        column = QVBoxLayout(self)
        column.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.setSpacing(10)
        column.addWidget(self._spinner)
        column.addSpacing(2)
        column.addWidget(self._message)
        column.addWidget(self._detail)

        self._anim = QTimer(self)
        self._anim.setInterval(SPIN_INTERVAL_MS)
        self._anim.timeout.connect(self._tick)

        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self.stop)

    # --- api --------------------------------------------------------------

    def start(self, message: str, detail: str = "") -> None:
        """Show the scrim. Calling it again just changes the wording."""
        self._active = True
        self._message.setText(message)
        self._detail.setText(detail)
        self._detail.setVisible(bool(detail))

        if self.parentWidget() is not None:
            self.setGeometry(self.parentWidget().rect())
        self.show()
        self.raise_()
        self._tick()
        self._anim.start()
        self._timeout.start(TIMEOUT_MS)

    def stop(self) -> None:
        self._active = False
        self._anim.stop()
        self._timeout.stop()
        self.hide()

    @property
    def busy(self) -> bool:
        return self._active

    # --- internals --------------------------------------------------------

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 — Qt naming
        """Stay the size of the parent, and above its siblings."""
        parent = self.parentWidget()
        if watched is parent and event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self.setGeometry(parent.rect())
            if self.isVisible():
                self.raise_()
        return super().eventFilter(watched, event)

    def paintEvent(self, event) -> None:  # noqa: N802 — Qt naming
        """Painted by hand: a stylesheet background would not give us alpha."""
        colour = QColor(theme.BG)
        colour.setAlpha(215)
        QPainter(self).fillRect(self.rect(), colour)

    def _tick(self) -> None:
        self._angle = (self._angle + 30) % 360
        icon = qta.icon("fa6s.spinner", color=theme.ACCENT, rotated=self._angle)
        self._spinner.setPixmap(icon.pixmap(30, 30))

    # Swallow input while visible. Being on top of the stack is not enough on its
    # own — a click could still land on a sibling that has grabbed the mouse.

    def mousePressEvent(self, event) -> None:  # noqa: N802 — Qt naming
        event.accept()

    def keyPressEvent(self, event) -> None:  # noqa: N802 — Qt naming
        event.accept()
