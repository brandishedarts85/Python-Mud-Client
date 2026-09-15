from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
    QDockWidget,
)

from persistence import (
    DEFAULT_MACROS_PATH, load_macros, profile_macros_path, save_macros,
)


_KEY_LABELS = {
    "7": "7", "8": "8", "9": "9", "divide": "/",
    "4": "4", "5": "5", "6": "6", "multiply": "*",
    "1": "1", "2": "2", "3": "3", "subtract": "-",
    "0": "0", "decimal": ".", "add": "+", "enter": "Enter",
}

_LAYOUT = (
    ("7", 0, 0, 1, 1), ("8", 0, 1, 1, 1), ("9", 0, 2, 1, 1), ("divide", 0, 3, 1, 1),
    ("4", 1, 0, 1, 1), ("5", 1, 1, 1, 1), ("6", 1, 2, 1, 1), ("multiply", 1, 3, 1, 1),
    ("1", 2, 0, 1, 1), ("2", 2, 1, 1, 1), ("3", 2, 2, 1, 1), ("subtract", 2, 3, 1, 1),
    ("0", 3, 0, 1, 2), ("decimal", 3, 2, 1, 1), ("add", 3, 3, 1, 1),
    ("enter", 4, 0, 1, 4),
)


class _MacroBindingDialog(QDialog):
    def __init__(self, key_label: str, binding: dict, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Numpad {key_label} Macro")
        self.label_edit = QLineEdit(binding.get("label", ""), self)
        self.command_edit = QLineEdit(binding.get("command", ""), self)
        self.enabled_box = QCheckBox("Binding enabled", self)
        self.enabled_box.setChecked(binding.get("enabled", True))

        form = QFormLayout()
        form.addRow("Button label", self.label_edit)
        form.addRow("Command", self.command_edit)
        form.addRow("", self.enabled_box)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def binding(self) -> dict:
        return {
            "label": self.label_edit.text().strip(),
            "command": self.command_edit.text(),
            "enabled": self.enabled_box.isChecked(),
        }


class MacroDock(QDockWidget):
    """Physical-numpad macro pad with base/Ctrl/Alt layers.

    Left-clicking an assigned key sends its command.  Right-clicking edits the
    assignment.  Unassigned keys are never intercepted from the physical
    keyboard, so normal numeric typing remains available even while the macro
    system is enabled.
    """

    command_requested = Signal(str)
    enabled_changed = Signal(bool)

    def __init__(self, parent=None, *, path: str = DEFAULT_MACROS_PATH) -> None:
        super().__init__("Numpad Macros", parent)
        self.setObjectName("MacroDock")
        self.global_path = path
        self.path = path
        self.profile_name: str | None = None
        try:
            self.config = load_macros(path)
        except Exception as exc:
            self.config = {"enabled": True, "bindings": {"base": {}, "ctrl": {}, "alt": {}}}
            QMessageBox.warning(
                parent,
                "Numpad Macros",
                f"Could not load {path}:\n{exc}\n\nUsing empty macro bindings for this run.",
            )

        body = QWidget(self)
        root = QVBoxLayout(body)

        self.enabled_box = QCheckBox("Enable Numpad Macros", body)
        self.enabled_box.setChecked(bool(self.config.get("enabled", True)))
        self.enabled_box.setToolTip(
            "Disable this to return every numpad key to normal numeric typing."
        )
        root.addWidget(self.enabled_box)

        self.layer_combo = QComboBox(body)
        self.layer_combo.addItem("Numpad", "base")
        self.layer_combo.addItem("Ctrl + Numpad", "ctrl")
        self.layer_combo.addItem("Alt + Numpad", "alt")
        root.addWidget(self.layer_combo)

        note = QLabel("Left-click fires • Right-click edits", body)
        root.addWidget(note)

        pad = QWidget(body)
        grid = QGridLayout(pad)
        grid.setSpacing(4)
        self.buttons: dict[str, QPushButton] = {}
        for key, row, col, row_span, col_span in _LAYOUT:
            button = QPushButton(pad)
            button.setMinimumHeight(46)
            button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            button.clicked.connect(
                lambda _checked=False, key_name=key: self._fire_clicked(key_name)
            )
            button.customContextMenuRequested.connect(
                lambda _pos, key_name=key: self._edit_binding(key_name)
            )
            self.buttons[key] = button
            grid.addWidget(button, row, col, row_span, col_span)
        root.addWidget(pad)
        self.setWidget(body)

        self.enabled_box.toggled.connect(self._enabled_toggled)
        self.layer_combo.currentIndexChanged.connect(self.refresh)
        self.refresh()

    def set_scope(self, profile_name: str | None) -> None:
        normalized = str(profile_name).strip() if profile_name is not None else None
        if not normalized:
            normalized = None
            target = self.global_path
        else:
            root = Path(self.global_path).parent / "profiles_data"
            target = profile_macros_path(normalized, root=root)
        if target == self.path and normalized == self.profile_name:
            return
        try:
            config = load_macros(target)
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Numpad Macros",
                f"Could not load {target}:\n{exc}\n\nUsing empty macro bindings for this run.",
            )
            config = {"enabled": True, "bindings": {"base": {}, "ctrl": {}, "alt": {}}}
        self.path = target
        self.profile_name = normalized
        self.config = config
        self.enabled_box.blockSignals(True)
        self.enabled_box.setChecked(bool(config.get("enabled", True)))
        self.enabled_box.blockSignals(False)
        self.refresh()
        self.enabled_changed.emit(self.enabled)

    @property
    def enabled(self) -> bool:
        return self.enabled_box.isChecked()

    @property
    def layer(self) -> str:
        return str(self.layer_combo.currentData())

    def set_enabled(self, enabled: bool) -> None:
        self.enabled_box.setChecked(bool(enabled))

    def _bindings(self, layer: str | None = None) -> dict:
        return self.config.setdefault("bindings", {}).setdefault(layer or self.layer, {})

    def _binding(self, key: str, layer: str | None = None) -> dict:
        return self._bindings(layer).get(key, {"label": "", "command": "", "enabled": True})

    def refresh(self) -> None:
        for key, button in self.buttons.items():
            binding = self._binding(key)
            command = binding.get("command", "")
            label = binding.get("label", "") or _KEY_LABELS[key]
            if command:
                button.setText(f"{label}\n{command}")
                button.setToolTip(f"{command}\nRight-click to edit")
            else:
                button.setText(f"{label}\nUnassigned")
                button.setToolTip("Unassigned — right-click to edit")
            active = self.enabled and binding.get("enabled", True)
            # Keep buttons interactive even while macros are disabled so a
            # right-click can still edit/re-enable an assignment.  Dim text
            # communicates that left-click/key activation is inactive.
            button.setEnabled(True)
            button.setStyleSheet("" if active else "color: palette(mid);")

    def _persist(self) -> bool:
        try:
            save_macros(self.config, self.path)
        except Exception as exc:
            QMessageBox.warning(self, "Numpad Macros", f"Could not save macros:\n{exc}")
            return False
        return True

    def _enabled_toggled(self, enabled: bool) -> None:
        previous = bool(self.config.get("enabled", True))
        self.config["enabled"] = bool(enabled)
        if not self._persist():
            self.config["enabled"] = previous
            self.enabled_box.blockSignals(True)
            self.enabled_box.setChecked(previous)
            self.enabled_box.blockSignals(False)
            self.refresh()
            return
        self.refresh()
        self.enabled_changed.emit(bool(enabled))

    def _edit_binding(self, key: str) -> None:
        dialog = _MacroBindingDialog(_KEY_LABELS[key], self._binding(key), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        binding = dialog.binding()
        if not binding["command"]:
            # Empty command means intentionally unassigned.
            binding["label"] = binding["label"] or ""
        bindings = self._bindings()
        had_previous = key in bindings
        previous = bindings.get(key)
        bindings[key] = binding
        if not self._persist():
            if had_previous:
                bindings[key] = previous
            else:
                bindings.pop(key, None)
        self.refresh()

    def _fire_clicked(self, key: str) -> None:
        if not self.enabled:
            return
        binding = self._binding(key)
        command = binding.get("command", "")
        if command and binding.get("enabled", True):
            self.command_requested.emit(command)

    @staticmethod
    def key_name_for_event(event) -> str | None:
        """Return our logical numpad key name for a physical keypad event.

        On Windows, PySide6 does not consistently include KeypadModifier for
        the dedicated number-pad digit keys on every keyboard/driver.  The
        native virtual-key codes *do* distinguish VK_NUMPAD0..9 and the
        keypad operators from the top-row keys, so prefer those when present.
        Keep Qt's KeypadModifier mapping as the portable fallback.
        """
        native_virtual_key = 0
        native_vk_getter = getattr(event, "nativeVirtualKey", None)
        if callable(native_vk_getter):
            try:
                native_virtual_key = int(native_vk_getter())
            except (TypeError, ValueError):
                native_virtual_key = 0

        # Windows virtual-key codes.  These are stable platform constants and
        # only match the dedicated numeric keypad, never the top number row.
        windows_vk_mapping = {
            0x60: "0",  # VK_NUMPAD0
            0x61: "1",  # VK_NUMPAD1
            0x62: "2",  # VK_NUMPAD2
            0x63: "3",  # VK_NUMPAD3
            0x64: "4",  # VK_NUMPAD4
            0x65: "5",  # VK_NUMPAD5
            0x66: "6",  # VK_NUMPAD6
            0x67: "7",  # VK_NUMPAD7
            0x68: "8",  # VK_NUMPAD8
            0x69: "9",  # VK_NUMPAD9
            0x6A: "multiply",  # VK_MULTIPLY
            0x6B: "add",       # VK_ADD
            0x6D: "subtract",  # VK_SUBTRACT
            0x6E: "decimal",   # VK_DECIMAL
            0x6F: "divide",    # VK_DIVIDE
        }
        native_match = windows_vk_mapping.get(native_virtual_key)
        if native_match is not None:
            return native_match

        mods = event.modifiers()
        if not (mods & Qt.KeyboardModifier.KeypadModifier):
            return None

        key = event.key()
        mapping = {
            Qt.Key.Key_0: "0",
            Qt.Key.Key_1: "1",
            Qt.Key.Key_2: "2",
            Qt.Key.Key_3: "3",
            Qt.Key.Key_4: "4",
            Qt.Key.Key_5: "5",
            Qt.Key.Key_6: "6",
            Qt.Key.Key_7: "7",
            Qt.Key.Key_8: "8",
            Qt.Key.Key_9: "9",
            Qt.Key.Key_Slash: "divide",
            Qt.Key.Key_Asterisk: "multiply",
            Qt.Key.Key_Minus: "subtract",
            Qt.Key.Key_Plus: "add",
            Qt.Key.Key_Period: "decimal",
            Qt.Key.Key_Comma: "decimal",
            Qt.Key.Key_Enter: "enter",
            # NumLock-off fallbacks used by Windows/Qt on some keyboards.
            Qt.Key.Key_Home: "7",
            Qt.Key.Key_Up: "8",
            Qt.Key.Key_PageUp: "9",
            Qt.Key.Key_Left: "4",
            Qt.Key.Key_Clear: "5",
            Qt.Key.Key_Right: "6",
            Qt.Key.Key_End: "1",
            Qt.Key.Key_Down: "2",
            Qt.Key.Key_PageDown: "3",
            Qt.Key.Key_Insert: "0",
            Qt.Key.Key_Delete: "decimal",
        }
        return mapping.get(key)

    @staticmethod
    def layer_for_event(event) -> str | None:
        mods = event.modifiers()
        if mods & Qt.KeyboardModifier.ShiftModifier:
            return None
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        alt = bool(mods & Qt.KeyboardModifier.AltModifier)
        if ctrl and alt:
            return None
        if ctrl:
            return "ctrl"
        if alt:
            return "alt"
        return "base"

    def handle_key_event(self, event) -> bool:
        """Handle a physical keypad press; return True only when consumed."""
        if not self.enabled:
            return False
        key = self.key_name_for_event(event)
        layer = self.layer_for_event(event)
        if key is None or layer is None:
            return False
        binding = self._binding(key, layer)
        command = binding.get("command", "")
        if not command or not binding.get("enabled", True):
            # Critical usability rule: unassigned keys remain ordinary numbers.
            return False
        self.command_requested.emit(command)
        event.accept()
        return True
