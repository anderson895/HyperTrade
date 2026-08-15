"""Window chrome shared by every page: the top bar, page headers, and status bar.

Modelled on the reference design: a breadcrumb and a connection pill along the top,
a titled header with its actions on the right, and a thin status strip at the foot.
"""

from __future__ import annotations

import datetime as dt

import qtawesome as qta
from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import theme


#: Every size below was measured off the reference screenshot rather than eyeballed:
#: the bar spans 68px, the page icon reads 32px, and the title's cap height of 19px
#: puts its font at 25px.
BAR_HEIGHT = 68
PAGE_ICON = 32
TITLE_SIZE = 25
CRUMB_SIZE = 15


def divider() -> QFrame:
    """A hairline rule, as used between the wordmark and the page breadcrumb."""
    line = QFrame()
    line.setFixedWidth(1)
    line.setStyleSheet(f"background: {theme.BORDER}; border: none;")
    return line


class TopBar(QFrame):
    """Sidebar toggle, a breadcrumb for the page, and the connection state."""

    def __init__(self) -> None:
        super().__init__()
        self.setProperty("topbar", True)
        self.setFixedHeight(BAR_HEIGHT)

        self.toggle_btn = QToolButton()
        self.toggle_btn.setIcon(qta.icon("fa6s.bars", color=theme.TEXT))
        self.toggle_btn.setIconSize(QSize(22, 22))
        self.toggle_btn.setToolTip("Toggle sidebar")
        self.toggle_btn.setAutoRaise(True)

        self._icon = QLabel()
        self._title = QLabel("Dashboard")
        self._title.setStyleSheet(
            f"color: {theme.BRAND_MINT}; font-size: {TITLE_SIZE}px;"
            f" font-weight: bold; background: transparent"
        )

        self._separator = QLabel("/")
        self._separator.setStyleSheet(
            f"color: {theme.MUTED}; font-size: {CRUMB_SIZE}px; background: transparent"
        )
        self._crumb = QLabel("")
        # White, not muted: the mockup gives the crumb the same weight as body text.
        self._crumb.setStyleSheet(
            f"color: {theme.TEXT}; font-size: {CRUMB_SIZE}px; background: transparent"
        )

        self.connection = _Pill("Connecting...")

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 0, 16, 0)
        row.setSpacing(10)
        row.addWidget(self.toggle_btn)
        row.addWidget(divider())
        row.addSpacing(2)
        row.addWidget(self._icon)
        row.addWidget(self._title)
        row.addWidget(self._separator)
        row.addWidget(self._crumb)
        row.addStretch()
        row.addWidget(self.connection)

    def set_page(self, icon: str, title: str, crumb: str) -> None:
        self._icon.setPixmap(
            qta.icon(icon, color=theme.BRAND_MINT).pixmap(PAGE_ICON, PAGE_ICON)
        )
        self._title.setText(title)
        self._crumb.setText(crumb)


def _dim(colour: str, amount: float = 0.35) -> str:
    """`colour` mixed down towards the bar background.

    The reference outlines its pill in a muted tint, not the full state colour; at
    full strength the outline competes with the dot it is meant to frame.
    """
    fg = [int(colour[i : i + 2], 16) for i in (1, 3, 5)]
    bg = [int(theme.BG[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{int(b + (f - b) * amount):02x}" for f, b in zip(fg, bg))


class _Pill(QFrame):
    """A dot and a caption in a rounded outline — the connection indicator."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.setProperty("pill", True)
        self.setFixedHeight(34)
        self._dot = QLabel("●")
        self._label = QLabel(text)
        # White and semi-bold: only the dot and the outline carry the state colour,
        # and full bold here reads as an alert the pill does not mean.
        self._label.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 14px; font-weight: 600;"
            f" background: transparent"
        )

        row = QHBoxLayout(self)
        row.setContentsMargins(13, 0, 15, 0)
        row.setSpacing(8)
        row.addWidget(self._dot)
        row.addWidget(self._label)
        self.set_state(None, text)

    def set_state(self, ok: bool | None, text: str) -> None:
        colour = theme.MUTED if ok is None else (theme.ACCENT if ok else theme.RED)
        self._dot.setStyleSheet(
            f"color: {colour}; font-size: 17px; background: transparent"
        )
        self._label.setText(text)
        self.setStyleSheet(
            f"QFrame[pill='true'] {{"
            f"  background: {theme.INPUT_BG};"
            f"  border: 1px solid {_dim(colour)};"
            # A rounded rectangle, not a capsule — half the height would round the
            # ends off into a stadium, which the reference does not do.
            f"  border-radius: 12px;"
            f"}}"
        )


class PageHeader(QWidget):
    """A large icon, a title and subtitle, and the page's actions on the right."""

    def __init__(self, icon: str, title: str, subtitle: str) -> None:
        super().__init__()

        badge = QLabel()
        badge.setPixmap(qta.icon(icon, color=theme.ACCENT).pixmap(30, 30))

        heading = QLabel(title)
        heading.setProperty("pagetitle", True)
        caption = QLabel(subtitle)
        caption.setProperty("muted", True)

        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(heading)
        text.addWidget(caption)

        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        row.addWidget(badge, alignment=Qt.AlignmentFlag.AlignTop)
        row.addLayout(text)
        row.addStretch()
        row.addLayout(self.actions)

    def add_action(self, widget: QWidget) -> None:
        self.actions.addWidget(widget)


class StatusBar(QFrame):
    """The thin strip along the foot: bot state on the left, network on the right."""

    def __init__(self) -> None:
        super().__init__()
        self.setProperty("statusbar", True)
        self.setFixedHeight(30)

        self._dot = QLabel("●")
        self._dot.setStyleSheet(f"color: {theme.MUTED}; background: transparent")
        self._state = QLabel("Bot Status:  Starting")
        self._state.setProperty("muted", True)

        self._network = QLabel("")
        self._network.setProperty("muted", True)
        self._clock = QLabel("")
        self._clock.setProperty("muted", True)

        row = QHBoxLayout(self)
        row.setContentsMargins(16, 0, 16, 0)
        row.setSpacing(8)
        row.addWidget(self._dot)
        row.addWidget(self._state)
        row.addStretch()
        row.addWidget(self._network)
        row.addWidget(QLabel("|"))
        row.addWidget(self._clock)

    def set_state(self, running: bool, ready: bool) -> None:
        if running:
            text, colour = "Running", theme.ACCENT
        elif ready:
            text, colour = "Ready", theme.ACCENT
        else:
            text, colour = "Not connected", theme.RED
        self._dot.setStyleSheet(f"color: {colour}; background: transparent")
        self._state.setText(f"Bot Status:  {text}")
        self._state.setStyleSheet(f"color: {colour}; background: transparent")

    def set_network(self, network: str, mode: str) -> None:
        self._network.setText(f"Network: {network}    Mode: {mode}")

    def tick_clock(self) -> None:
        self._clock.setText(dt.datetime.now().strftime("%H:%M:%S"))
