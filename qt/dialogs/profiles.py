from __future__ import annotations

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QMessageBox, QPushButton,
    QSpinBox, QSplitter, QVBoxLayout, QWidget,
)

from persistence import delete_profile, load_profiles_safely, save_profile


class ProfilesDialog(QDialog):
    profile_selected = Signal(str, object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Connection Profiles")
        self.resize(720, 420)
        self._profiles = {}

        self.list = QListWidget(self)
        self.list.currentTextChanged.connect(self._load_selected)

        editor = QWidget(self)
        form = QFormLayout(editor)
        self.name_edit = QLineEdit(editor)
        self.host_edit = QLineEdit(editor)
        self.port_edit = QSpinBox(editor); self.port_edit.setRange(1, 65535)
        self.terminal_edit = QLineEdit("xterm-256color", editor)
        self.reconnect_check = QCheckBox("Automatic reconnect", editor); self.reconnect_check.setChecked(True)
        self.base_delay = QDoubleSpinBox(editor); self.base_delay.setRange(0.1, 3600.0); self.base_delay.setValue(3.0); self.base_delay.setSuffix(" s")
        self.max_delay = QDoubleSpinBox(editor); self.max_delay.setRange(0.1, 3600.0); self.max_delay.setValue(60.0); self.max_delay.setSuffix(" s")
        form.addRow("Profile name", self.name_edit)
        form.addRow("Host", self.host_edit)
        form.addRow("Port", self.port_edit)
        form.addRow("Terminal type", self.terminal_edit)
        form.addRow("", self.reconnect_check)
        form.addRow("Base reconnect delay", self.base_delay)
        form.addRow("Max reconnect delay", self.max_delay)

        splitter = QSplitter(self); splitter.addWidget(self.list); splitter.addWidget(editor); splitter.setStretchFactor(1, 1)
        new_button = QPushButton("New", self); new_button.clicked.connect(self._new_profile)
        save_button = QPushButton("Save", self); save_button.clicked.connect(self._save_profile)
        delete_button = QPushButton("Delete", self); delete_button.clicked.connect(self._delete_profile)
        use_button = QPushButton("Use for Active Session", self); use_button.clicked.connect(self._use_profile)
        row = QHBoxLayout(); row.addWidget(new_button); row.addWidget(save_button); row.addWidget(delete_button); row.addStretch(1); row.addWidget(use_button)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=self); close.rejected.connect(self.reject)
        layout = QVBoxLayout(self); layout.addWidget(splitter); layout.addLayout(row); layout.addWidget(close)
        self._reload()

    def _reload(self) -> None:
        profiles, error = load_profiles_safely()
        if error is not None:
            QMessageBox.warning(
                self,
                "Profiles",
                "Could not load profiles. The file was left untouched.\n\n" + error,
            )
        self._profiles = profiles
        current = self.list.currentItem().text() if self.list.currentItem() else None
        self.list.clear(); self.list.addItems(sorted(self._profiles))
        if current:
            matches = self.list.findItems(current, Qt.MatchFlag.MatchExactly)
            if matches: self.list.setCurrentItem(matches[0])

    def _new_profile(self) -> None:
        self.list.clearSelection(); self.name_edit.clear(); self.host_edit.clear(); self.port_edit.setValue(4000)
        self.terminal_edit.setText("xterm-256color"); self.reconnect_check.setChecked(True); self.base_delay.setValue(3.0); self.max_delay.setValue(60.0)
        self.name_edit.setFocus()

    def _load_selected(self, name: str) -> None:
        profile = self._profiles.get(name)
        if not profile: return
        self.name_edit.setText(name); self.host_edit.setText(profile["host"]); self.port_edit.setValue(profile["port"]); self.terminal_edit.setText(profile["terminal_type"])
        self.reconnect_check.setChecked(profile["auto_reconnect"]); self.base_delay.setValue(profile["reconnect_base_delay"]); self.max_delay.setValue(profile["reconnect_max_delay"])

    def _settings(self) -> tuple[str, dict]:
        return self.name_edit.text().strip(), {
            "host": self.host_edit.text().strip(), "port": self.port_edit.value(), "terminal_type": self.terminal_edit.text().strip() or "xterm-256color",
            "auto_reconnect": self.reconnect_check.isChecked(), "reconnect_base_delay": self.base_delay.value(), "reconnect_max_delay": self.max_delay.value(),
        }

    def _save_profile(self) -> None:
        name, data = self._settings()
        if not name or not data["host"]:
            QMessageBox.warning(self, "Profiles", "Profile name and host are required."); return
        if data["reconnect_max_delay"] < data["reconnect_base_delay"]:
            QMessageBox.warning(self, "Profiles", "Maximum reconnect delay must be at least the base delay."); return
        try:
            save_profile(name, **data)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Profiles", f"Could not save profile:\n{exc}")
            return
        self._reload()
        matches = self.list.findItems(name, Qt.MatchFlag.MatchExactly)
        if matches: self.list.setCurrentItem(matches[0])

    def _delete_profile(self) -> None:
        item = self.list.currentItem()
        if item is None:
            return
        name = item.text()
        if QMessageBox.question(
            self, "Profiles", f"Delete profile {name!r}?"
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            delete_profile(name)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Profiles", f"Could not delete profile:\n{exc}")
            return
        self._reload()
        self._new_profile()

    def _use_profile(self) -> None:
        name, data = self._settings()
        if not data["host"]:
            QMessageBox.warning(self, "Profiles", "Select or enter a profile first."); return
        self.profile_selected.emit(name, data); self.accept()
