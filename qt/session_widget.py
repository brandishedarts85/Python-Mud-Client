"""Central widget for one logical MUD session."""

from __future__ import annotations

from pathlib import Path
from contextlib import suppress

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from qt.command_input import CommandInput
from qt.find_bar import FindBar
from qt.output_view import MudOutputView
from qt.session_bridge import QtSessionBridge
from client_settings import ClientSettings
from session_controller import MudSessionController
from transcript import TranscriptLogger


class MudSessionWidget(QWidget):
    logging_error = Signal(str)

    def __init__(self, controller: MudSessionController, parent=None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.bridge = QtSessionBridge(controller, self)

        self.output = MudOutputView(self)
        self.find_bar = FindBar(self)
        self.find_bar.hide()
        self.command_input = CommandInput(self)
        self._transcript_logger: TranscriptLogger | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.output, 1)
        layout.addWidget(self.find_bar, 0)
        layout.addWidget(self.command_input, 0)

        self.bridge.line_received.connect(self._append_visible_line)
        self.bridge.system_line.connect(self._append_visible_line)
        self.command_input.command_submitted.connect(controller.submit_command)
        self.command_input.history_previous_requested.connect(self._history_previous)
        self.command_input.history_next_requested.connect(self._history_next)
        self.find_bar.next_requested.connect(self._find_next)
        self.find_bar.previous_requested.connect(self._find_previous)
        self.find_bar.close_requested.connect(self.hide_find_bar)

    def apply_settings(self, settings: ClientSettings) -> None:
        self.output.apply_appearance(
            font_family=settings.font_family,
            font_size=settings.font_size,
            foreground=settings.output_foreground,
            background=settings.output_background,
            scrollback_blocks=settings.scrollback_blocks,
            timestamps=settings.timestamps,
        )
        self.controller.local_echo_enabled = settings.local_echo
        self.controller.set_scrollback_limit(settings.scrollback_blocks)

    def _append_visible_line(self, line) -> None:
        logger = self._transcript_logger
        if logger is not None:
            try:
                logger.write_line(line.plain_text())
            except OSError as exc:
                # Logging is presentation support: a disk failure must never
                # interrupt incoming MUD text or the session transport.
                self._transcript_logger = None
                with suppress(OSError):
                    logger.close()
                self.logging_error.emit(str(exc))
        self.output.append_styled_line(line)

    @property
    def logging_active(self) -> bool:
        return self._transcript_logger is not None and self._transcript_logger.active

    @property
    def log_path(self) -> Path | None:
        logger = self._transcript_logger
        return logger.path if logger is not None else None

    def start_logging(self, path: str | Path) -> None:
        self.stop_logging()
        logger = TranscriptLogger(path)
        logger.start()
        self._transcript_logger = logger

    def stop_logging(self) -> None:
        logger, self._transcript_logger = self._transcript_logger, None
        if logger is not None:
            try:
                logger.close()
            except OSError as exc:
                self.logging_error.emit(str(exc))

    def show_find_bar(self) -> None:
        self.find_bar.show()
        self.find_bar.focus_query()

    def hide_find_bar(self) -> None:
        self.find_bar.hide()
        self.command_input.setFocus()

    def _find_next(self, query: str, case_sensitive: bool) -> None:
        found = self.output.find_text(query, case_sensitive=case_sensitive)
        self.find_bar.status.setText("" if found else "Not found")

    def _find_previous(self, query: str, case_sensitive: bool) -> None:
        found = self.output.find_text(
            query, backward=True, case_sensitive=case_sensitive
        )
        self.find_bar.status.setText("" if found else "Not found")

    def _history_previous(self) -> None:
        value = self.controller.history_previous(self.command_input.text())
        if value is not None:
            self.command_input.setText(value)
            self.command_input.end(False)

    def _history_next(self) -> None:
        self.command_input.setText(self.controller.history_next())
        self.command_input.end(False)
    def dispose(self) -> None:
        """Release logging and controller subscriptions before destruction."""
        self.stop_logging()
        self.bridge.dispose()

