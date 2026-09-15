"""Small per-session transcript find bar."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QWidget


class FindBar(QWidget):
    next_requested = Signal(str, bool)
    previous_requested = Signal(str, bool)
    close_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.query = QLineEdit(self)
        self.query.setPlaceholderText("Find in transcript…")
        self.case_sensitive = QCheckBox("Match case", self)
        self.status = QLabel("", self)
        previous = QPushButton("Previous", self)
        next_button = QPushButton("Next", self)
        close = QPushButton("×", self)
        close.setToolTip("Close search")
        close.setMaximumWidth(32)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.addWidget(self.query, 1)
        layout.addWidget(self.case_sensitive)
        layout.addWidget(previous)
        layout.addWidget(next_button)
        layout.addWidget(self.status)
        layout.addWidget(close)

        self.query.returnPressed.connect(self._next)
        next_button.clicked.connect(self._next)
        previous.clicked.connect(self._previous)
        close.clicked.connect(self.close_requested)

    def _next(self) -> None:
        self.next_requested.emit(self.query.text(), self.case_sensitive.isChecked())

    def _previous(self) -> None:
        self.previous_requested.emit(self.query.text(), self.case_sensitive.isChecked())

    def focus_query(self) -> None:
        self.query.setFocus()
        self.query.selectAll()
