from __future__ import annotations

from contextlib import suppress

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QDockWidget,
)

from lifecycle import Subscription
from session_controller import MudSessionController
from variables import VariableEntry, parse_typed_value


class _VariableDialog(QDialog):
    def __init__(
        self,
        entry: VariableEntry | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Variable")

        root = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit(self)
        self.type_combo = QComboBox(self)
        for label, key in (
            ("String", "string"),
            ("Integer", "int"),
            ("Float", "float"),
            ("Boolean", "bool"),
        ):
            self.type_combo.addItem(label, key)
        self.value_edit = QLineEdit(self)
        form.addRow("Name", self.name_edit)
        form.addRow("Type", self.type_combo)
        form.addRow("Value", self.value_edit)
        root.addLayout(form)

        hint = QLabel("Use ${name} in outbound commands, aliases, triggers, or macros.", self)
        hint.setWordWrap(True)
        root.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        if entry is not None:
            self.name_edit.setText(entry.name)
            index = self.type_combo.findData(entry.type_name)
            if index >= 0:
                self.type_combo.setCurrentIndex(index)
            if entry.type_name == "bool":
                self.value_edit.setText("true" if entry.value else "false")
            else:
                self.value_edit.setText(str(entry.value))

    def _accept_if_valid(self) -> None:
        try:
            self.variable()
        except ValueError as exc:
            QMessageBox.warning(self, "Variable", str(exc))
            return
        self.accept()

    def variable(self) -> tuple[str, object]:
        name = self.name_edit.text().strip()
        if not name:
            raise ValueError("Variable name must not be empty.")
        value = parse_typed_value(
            str(self.type_combo.currentData()),
            self.value_edit.text(),
        )
        return name, value


class VariablesDock(QDockWidget):
    """Profile-aware variable editor bound to the active session controller."""

    def __init__(self, parent=None) -> None:
        super().__init__("Variables", parent)
        self.setObjectName("VariablesDock")
        self.controller: MudSessionController | None = None
        self._subscription: Subscription | None = None

        body = QWidget(self)
        root = QVBoxLayout(body)
        self.scope_label = QLabel("Scope: —", body)
        root.addWidget(self.scope_label)

        self.table = QTableWidget(0, 3, body)
        self.table.setHorizontalHeaderLabels(("Name", "Type", "Value"))
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.doubleClicked.connect(self._edit_selected)
        root.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        self.add_button = QPushButton("Add…", body)
        self.edit_button = QPushButton("Edit…", body)
        self.remove_button = QPushButton("Remove", body)
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.edit_button)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)
        root.addLayout(buttons)

        self.add_button.clicked.connect(self._add)
        self.edit_button.clicked.connect(self._edit_selected)
        self.remove_button.clicked.connect(self._remove_selected)
        self.setWidget(body)
        self.refresh()

    def dispose(self) -> None:
        if self._subscription is not None:
            self._subscription.close()
            self._subscription = None
        self.controller = None

    def set_controller(self, controller: MudSessionController | None) -> None:
        if controller is self.controller:
            self.refresh()
            return
        if self._subscription is not None:
            self._subscription.close()
            self._subscription = None
        self.controller = controller
        if controller is not None:
            self._subscription = controller.on("variables", lambda _payload: self.refresh())
        self.refresh()

    def _selected_name(self) -> str | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.text() if item is not None else None

    def _selected_entry(self) -> VariableEntry | None:
        controller = self.controller
        name = self._selected_name()
        if controller is None or name is None:
            return None
        for entry in controller.variables.items():
            if entry.name == name:
                return entry
        return None

    def refresh(self) -> None:
        controller = self.controller
        entries = () if controller is None else controller.variables.items()
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            values = (entry.name, entry.type_name, str(entry.value))
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, entry.name)
                self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()
        self.scope_label.setText(
            "Scope: —" if controller is None else f"Scope: {controller.variable_scope_label}"
        )
        enabled = controller is not None
        self.add_button.setEnabled(enabled)
        self.edit_button.setEnabled(enabled and bool(entries))
        self.remove_button.setEnabled(enabled and bool(entries))

    def _add(self) -> None:
        controller = self.controller
        if controller is None:
            return
        dialog = _VariableDialog(parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, value = dialog.variable()
        try:
            controller.set_variable(name, value)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Variables", f"Could not save variable:\n{exc}")

    def _edit_selected(self, *_args) -> None:
        controller = self.controller
        entry = self._selected_entry()
        if controller is None or entry is None:
            return
        dialog = _VariableDialog(entry, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, value = dialog.variable()
        try:
            controller.replace_variable(entry.name, name, value)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Variables", f"Could not save variable:\n{exc}")

    def _remove_selected(self) -> None:
        controller = self.controller
        name = self._selected_name()
        if controller is None or name is None:
            return
        try:
            controller.remove_variable(name)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Variables", f"Could not remove variable:\n{exc}")
