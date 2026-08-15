"""Logs page — the same lines that reach data/app.log, already redacted."""

from __future__ import annotations

import logging

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..logging_setup import LogLine
from . import theme

MAX_ROWS = 1_000


class LogsPage(QWidget):
    def __init__(self) -> None:
        super().__init__()

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Time", "Level", "Message"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        title = QLabel("Logs")
        title.setProperty("accent", True)
        hint = QLabel("Also written to data/app.log - send that file if something breaks.")
        hint.setProperty("muted", True)

        head = QHBoxLayout()
        head.addWidget(title)
        head.addStretch()
        head.addWidget(hint)

        root = QVBoxLayout(self)
        root.addLayout(head)
        root.addWidget(self.table, stretch=1)

    def add_log(self, line: LogLine) -> None:
        self.table.insertRow(0)
        for column, value in enumerate((line.time, line.level, line.message)):
            item = QTableWidgetItem(value)
            if line.levelno >= logging.ERROR:
                item.setForeground(QColor(theme.RED))
            elif line.levelno >= logging.WARNING:
                item.setForeground(QColor(theme.AMBER))
            self.table.setItem(0, column, item)

        while self.table.rowCount() > MAX_ROWS:
            self.table.removeRow(self.table.rowCount() - 1)
