"""Reusable card widgets, matching PolyTrade Pro's set."""

from __future__ import annotations

import qtawesome as qta
from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout

from . import theme


class WheelBlocker(QObject):
    """Blocks the mouse wheel on spin boxes and dropdowns.

    Scrolling a page with the cursor over an input silently changes its value.
    That is dangerous for Risk Per Trade or Leverage, so the wheel is ignored;
    typing, clicking, and the +/- buttons still work.
    """

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 — Qt naming
        if event.type() == QEvent.Type.Wheel:
            return True
        return super().eventFilter(obj, event)


class Card(QFrame):
    def __init__(self) -> None:
        super().__init__()
        self.setProperty("card", True)


class StatusCard(Card):
    """Connection card: icon, name, Connected/Disconnected, and a dot."""

    def __init__(self, icon: str, name: str, icon_color: str = theme.MUTED) -> None:
        super().__init__()
        self._icon = QLabel()
        self._icon.setPixmap(qta.icon(icon, color=icon_color).pixmap(26, 26))
        self._name = QLabel(name)
        self._name.setStyleSheet("font-weight: bold; font-size: 14px")
        self._sub = QLabel("Checking...")
        self._sub.setProperty("muted", True)
        self._dot = QLabel("●")
        self._dot.setStyleSheet(f"color: {theme.MUTED}; font-size: 15px")

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        text_col.addWidget(self._name)
        text_col.addWidget(self._sub)

        row = QHBoxLayout(self)
        row.setContentsMargins(14, 12, 14, 12)
        row.addWidget(self._icon)
        row.addLayout(text_col, stretch=1)
        row.addWidget(self._dot)

    def set_name(self, name: str) -> None:
        self._name.setText(name)

    def set_state(self, up: bool, text: str | None = None) -> None:
        color = theme.GREEN if up else theme.RED
        self._sub.setText(text or ("Connected" if up else "Disconnected"))
        self._sub.setStyleSheet(f"color: {color}")
        self._dot.setStyleSheet(f"color: {color}; font-size: 15px")


class StatCard(Card):
    """Title, a large value, and an optional sub-line."""

    def __init__(self, title: str, value: str = "-", sub: str = "") -> None:
        super().__init__()
        self._title = QLabel(title)
        self._title.setProperty("muted", True)
        self._value = QLabel(value)
        self._value.setStyleSheet("font-size: 20px; font-weight: bold")
        self._sub = QLabel(sub)
        self._sub.setProperty("muted", True)

        col = QVBoxLayout(self)
        col.setContentsMargins(14, 12, 14, 12)
        col.setSpacing(3)
        col.addWidget(self._title)
        col.addWidget(self._value)
        col.addWidget(self._sub)
        self._sub.setVisible(bool(sub))

    def set_value(self, text: str, color: str | None = None) -> None:
        self._value.setText(text)
        style = "font-size: 20px; font-weight: bold"
        if color:
            style += f"; color: {color}"
        self._value.setStyleSheet(style)

    def set_title(self, text: str) -> None:
        self._title.setText(text)

    def set_sub(self, text: str) -> None:
        self._sub.setText(text)
        self._sub.setVisible(bool(text))


def labelled_column(title: str, value: str) -> tuple[QVBoxLayout, QLabel]:
    """A muted caption above a bold value — the bottom bar's repeating unit."""
    caption = QLabel(title)
    caption.setProperty("muted", True)
    label = QLabel(value)
    label.setStyleSheet("font-weight: bold")

    column = QVBoxLayout()
    column.setSpacing(1)
    column.addWidget(caption)
    column.addWidget(label)
    return column, label
