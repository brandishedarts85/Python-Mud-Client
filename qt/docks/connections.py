from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDockWidget,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class ConnectionsDock(QDockWidget):
    connect_requested = Signal()
    disconnect_requested = Signal()
    reconnect_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__("Connections", parent)
        self.setObjectName("ConnectionsDock")

        body = QWidget(self)
        outer = QVBoxLayout(body)
        form = QFormLayout()

        self.host_label = QLabel("—")
        self.port_label = QLabel("—")
        self.state_label = QLabel("disconnected")
        self.terminal_label = QLabel("—")
        self.reconnect_label = QLabel("—")
        self.profile_label = QLabel("Global / manual")
        form.addRow("Host", self.host_label)
        form.addRow("Port", self.port_label)
        form.addRow("State", self.state_label)
        form.addRow("Terminal", self.terminal_label)
        form.addRow("Reconnect", self.reconnect_label)
        form.addRow("Automation scope", self.profile_label)
        outer.addLayout(form)

        buttons = QHBoxLayout()
        connect_button = QPushButton("Connect…")
        reconnect_button = QPushButton("Reconnect")
        disconnect_button = QPushButton("Disconnect")
        connect_button.clicked.connect(lambda: self.connect_requested.emit())
        reconnect_button.clicked.connect(lambda: self.reconnect_requested.emit())
        disconnect_button.clicked.connect(lambda: self.disconnect_requested.emit())
        buttons.addWidget(connect_button)
        buttons.addWidget(reconnect_button)
        buttons.addWidget(disconnect_button)
        outer.addLayout(buttons)
        outer.addStretch(1)

        self.setWidget(body)

    def set_session_state(self, state) -> None:
        self.host_label.setText(state.host or "—")
        self.port_label.setText(str(state.port) if state.port is not None else "—")
        self.state_label.setText(state.status_text)

    def set_controller(self, controller) -> None:
        if controller is None:
            self.terminal_label.setText("—")
            self.reconnect_label.setText("—")
            self.profile_label.setText("—")
            return
        self.set_session_state(controller.state)
        self.terminal_label.setText(controller.terminal_type)
        self.profile_label.setText(controller.automation_scope_label)
        if controller.auto_reconnect:
            self.reconnect_label.setText(
                f"on ({controller.reconnect_base_delay:g}s–{controller.reconnect_max_delay:g}s)"
            )
        else:
            self.reconnect_label.setText("off")
