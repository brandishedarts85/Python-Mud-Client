"""Command entry widget with session-owned history navigation."""

from __future__ import annotations

from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QLineEdit


class CommandInput(QLineEdit):
    command_submitted = Signal(str)
    history_previous_requested = Signal()
    history_next_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setPlaceholderText("Type a command and press Enter")
        self.returnPressed.connect(self._submit)

    def _submit(self) -> None:
        command = self.text()
        self.clear()
        self.command_submitted.emit(command)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Up:
            self.history_previous_requested.emit()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Down:
            self.history_next_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)
