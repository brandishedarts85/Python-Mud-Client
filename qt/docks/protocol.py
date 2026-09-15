from __future__ import annotations

import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDockWidget,
    QHeaderView,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)


TELNET_OPTION_NAMES = {
    1: "ECHO",
    3: "SGA",
    24: "TTYPE",
    25: "EOR",
    31: "NAWS",
    69: "MSDP",
    86: "MCCP2",
    201: "GMCP",
}


class ProtocolDock(QDockWidget):
    """Live protocol inspector for the active session."""

    def __init__(self, parent=None) -> None:
        super().__init__("Protocol", parent)
        self.setObjectName("ProtocolDock")

        self.tabs = QTabWidget(self)
        self.gmcp = self._viewer()
        self.msdp = self._viewer()
        self.telnet = self._telnet_table()
        self.events = self._viewer()

        self.tabs.addTab(self.gmcp, "GMCP")
        self.tabs.addTab(self.msdp, "MSDP")
        self.tabs.addTab(self.telnet, "Telnet")
        self.tabs.addTab(self.events, "Events")
        self.setWidget(self.tabs)

    @staticmethod
    def _viewer() -> QPlainTextEdit:
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        return view

    @staticmethod
    def _telnet_table() -> QTableWidget:
        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(["Option", "Code", "Peer state", "Meaning"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        return table

    def set_protocol_state(self, protocol) -> None:
        if protocol is None:
            self.gmcp.clear()
            self.msdp.clear()
            self.telnet.setRowCount(0)
            self.events.clear()
            return

        self.gmcp.setPlainText(self._dump(protocol.gmcp))
        self.msdp.setPlainText(self._dump(protocol.msdp))
        self._populate_telnet(protocol.telnet_options)
        self.events.setPlainText(self._format_events(protocol.recent_events))

    def _populate_telnet(self, options: dict[int, str]) -> None:
        rows = sorted(options.items(), key=lambda item: item[0])
        self.telnet.setRowCount(len(rows))

        for row, (code, state) in enumerate(rows):
            name = TELNET_OPTION_NAMES.get(code, f"OPTION-{code}")
            meaning = self._state_meaning(state)
            values = (name, str(code), state.upper(), meaning)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 1:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.telnet.setItem(row, column, item)

    @staticmethod
    def _state_meaning(state: str) -> str:
        return {
            "will": "Server enabled this option",
            "wont": "Server disabled this option",
            "do": "Server asked the client to enable this option",
            "dont": "Server asked the client to disable this option",
        }.get(state.lower(), state)

    @classmethod
    def _format_events(cls, events: list[dict]) -> str:
        if not events:
            return "No protocol events observed yet."

        lines: list[str] = []
        for event in events:
            kind = event.get("kind", "Protocol")
            data = event.get("data")
            if kind == "Telnet" and isinstance(data, dict):
                code = data.get("option")
                state = str(data.get("state", ""))
                name = TELNET_OPTION_NAMES.get(code, f"OPTION-{code}")
                lines.append(f"Telnet  {name} ({code})  {state.upper()}")
                continue
            if kind == "GMCP" and isinstance(data, dict):
                package = data.get("package", "?")
                payload = cls._compact(data.get("data"))
                lines.append(f"GMCP    {package}  {payload}")
                continue
            if kind == "MSDP" and isinstance(data, dict):
                variable = data.get("variable", "?")
                payload = cls._compact(data.get("value"))
                lines.append(f"MSDP    {variable} = {payload}")
                continue
            lines.append(f"{kind:<7} {cls._compact(data)}")
        return "\n".join(lines)

    @staticmethod
    def _compact(value) -> str:
        return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))

    @staticmethod
    def _dump(value) -> str:
        if not value:
            return "{}"
        return json.dumps(value, indent=2, sort_keys=True, default=str)
