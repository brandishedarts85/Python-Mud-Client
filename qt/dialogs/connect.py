from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QLineEdit, QSpinBox, QVBoxLayout,
)


class ConnectDialog(QDialog):
    def __init__(
        self,
        host: str = "",
        port: int = 4000,
        terminal_type: str = "xterm-256color",
        auto_reconnect: bool = True,
        reconnect_base_delay: float = 3.0,
        reconnect_max_delay: float = 60.0,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Connect")

        self.host_edit = QLineEdit(host, self)
        self.port_edit = QSpinBox(self)
        self.port_edit.setRange(1, 65535)
        self.port_edit.setValue(port)
        self.terminal_edit = QLineEdit(terminal_type, self)
        self.reconnect_check = QCheckBox("Reconnect automatically after unexpected disconnects", self)
        self.reconnect_check.setChecked(auto_reconnect)
        self.base_delay = QDoubleSpinBox(self)
        self.base_delay.setRange(0.1, 3600.0)
        self.base_delay.setValue(reconnect_base_delay)
        self.base_delay.setSuffix(" s")
        self.max_delay = QDoubleSpinBox(self)
        self.max_delay.setRange(0.1, 3600.0)
        self.max_delay.setValue(reconnect_max_delay)
        self.max_delay.setSuffix(" s")

        form = QFormLayout()
        form.addRow("Host", self.host_edit)
        form.addRow("Port", self.port_edit)
        form.addRow("Terminal type", self.terminal_edit)
        form.addRow("", self.reconnect_check)
        form.addRow("Reconnect base delay", self.base_delay)
        form.addRow("Reconnect max delay", self.max_delay)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def connection_settings(self) -> dict:
        return {
            "host": self.host_edit.text().strip(),
            "port": self.port_edit.value(),
            "terminal_type": self.terminal_edit.text().strip() or "xterm-256color",
            "auto_reconnect": self.reconnect_check.isChecked(),
            "reconnect_base_delay": self.base_delay.value(),
            "reconnect_max_delay": self.max_delay.value(),
        }

    def target(self) -> tuple[str, int]:
        settings = self.connection_settings()
        return settings["host"], settings["port"]
