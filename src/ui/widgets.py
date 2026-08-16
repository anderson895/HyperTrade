"""Reusable card widgets, matching PolyTrade Pro's set."""

from __future__ import annotations

import qtawesome as qta
from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from . import theme

#: Cards share the width equally and grow with the window; this is the narrowest
#: each may become. Fixed widths left a third card hanging off the edge of a 1672
#: window and a wasted strip on a 1920 one.
MIN_CARD_WIDTH = 420
#: The narrowest a note can be: the card's minimum, less its margins and border.
NOTE_WIDTH = MIN_CARD_WIDTH - 38


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


def wrapped_label(text: str = "") -> QLabel:
    """A word-wrapped label whose full height its layout will actually honour.

    QLabel can work out how tall it needs to be for a given width, but a layout only
    asks when the size policy says to. Without that flag the label is allocated its
    one-line size hint and the last line is quietly clipped — which cost one note its
    final sentence, one pixel short of fitting.
    """
    label = QLabel(text)
    label.setWordWrap(True)
    policy = label.sizePolicy()
    policy.setHeightForWidth(True)
    label.setSizePolicy(policy)
    return label


def note_label(text: str = "", width: int = NOTE_WIDTH) -> QLabel:
    """A muted, wrapped note that will not be squeezed out of its last line.

    The height is reserved for the narrowest the card can be. Any wider and the text
    wraps into fewer lines, which fits inside the space already set aside — so the
    label can grow with the card without ever being clipped.
    """
    note = wrapped_label(text)
    note.setProperty("muted", True)
    note.ensurePolished()  # or the metrics come from the unstyled font
    note.setMinimumHeight(
        note.fontMetrics()
        .boundingRect(0, 0, width, 0, Qt.TextFlag.TextWordWrap, text)
        .height()
    )
    return note


def note_box(title: str, body: str) -> QFrame:
    """The tinted callout from the reference design."""
    box = QFrame()
    box.setProperty("notebox", True)

    icon = QLabel()
    icon.setPixmap(qta.icon("fa6s.circle-info", color=theme.ACCENT).pixmap(14, 14))
    icon.setStyleSheet("background: transparent")
    heading = QLabel(title)
    heading.setStyleSheet(f"color: {theme.ACCENT}; font-weight: bold; background: transparent")

    head = QHBoxLayout()
    head.setSpacing(6)
    head.addWidget(icon)
    head.addWidget(heading)
    head.addStretch()

    text = note_label(body, NOTE_WIDTH - 24)
    text.setStyleSheet(f"color: {theme.ACCENT_TEXT}; background: transparent")

    column = QVBoxLayout(box)
    column.setContentsMargins(12, 10, 12, 10)
    column.setSpacing(4)
    column.addLayout(head)
    column.addWidget(text)
    return box


class TitledCard(Card):
    """A card headed by an icon and a title, holding fields in one or two columns."""

    def __init__(self, icon: str, title: str) -> None:
        super().__init__()
        self.setMinimumWidth(MIN_CARD_WIDTH)

        badge = QLabel()
        badge.setPixmap(qta.icon(icon, color=theme.ACCENT).pixmap(18, 18))
        badge.setStyleSheet("background: transparent")
        heading = QLabel(title)
        heading.setProperty("h3", True)
        heading.setStyleSheet("background: transparent")

        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(badge)
        head.addWidget(heading)
        head.addStretch()

        self._column = QVBoxLayout(self)
        self._column.setContentsMargins(18, 16, 18, 18)
        self._column.setSpacing(6)
        self._column.addLayout(head)
        self._column.addSpacing(8)

        self._grid: QGridLayout | None = None
        self._row = 0

    # --- single column ---------------------------------------------------

    def field(self, label: str, widget: QWidget) -> QLabel:
        caption = QLabel(label)
        caption.setProperty("muted", True)
        caption.setContentsMargins(0, 8, 0, 0)
        self._column.addWidget(caption)
        self._column.addWidget(widget)
        return caption

    def note(self, text: str = "") -> QLabel:
        note = note_label(text)
        self._column.addWidget(note)
        return note

    def add(self, widget: QWidget) -> None:
        self._column.addWidget(widget)

    def finish(self) -> None:
        self._column.addStretch()

    # --- two columns -----------------------------------------------------

    def start_grid(self) -> None:
        self._grid = QGridLayout()
        self._grid.setHorizontalSpacing(14)
        self._grid.setVerticalSpacing(4)
        self._column.addLayout(self._grid)
        self._row = 0

    def grid_field(
        self, label: str, widget: QWidget, column: int, span: int = 1
    ) -> QLabel:
        """Returns the caption so a field that applies in only one mode can hide its
        label along with itself. A stranded caption is worse than neither."""
        caption = QLabel(label)
        caption.setProperty("muted", True)
        caption.setContentsMargins(0, 8, 0, 0)
        self._grid.addWidget(caption, self._row, column, 1, span)
        self._grid.addWidget(widget, self._row + 1, column, 1, span)
        if column == 1 or span == 2:
            self._row += 2
        return caption


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
