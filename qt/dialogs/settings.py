from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFontComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from client_settings import ClientSettings


class _ColorButton(QPushButton):
    color_changed = Signal(str)

    def __init__(self, color: str, parent=None) -> None:
        super().__init__(parent)
        self._color = color
        self.clicked.connect(self._choose)
        self._refresh()

    @property
    def color(self) -> str:
        return self._color

    def set_color(self, color: str) -> None:
        self._color = color.lower()
        self._refresh()

    def _choose(self) -> None:
        chosen = QColorDialog.getColor(QColor(self._color), self, "Choose Color")
        if not chosen.isValid():
            return
        self.set_color(chosen.name())
        self.color_changed.emit(self._color)

    def _refresh(self) -> None:
        self.setText(self._color.upper())
        self.setStyleSheet(
            f"QPushButton {{ background: {self._color}; color: "
            f"{'#000000' if QColor(self._color).lightness() > 140 else '#ffffff'}; }}"
        )


class SettingsDialog(QDialog):
    """Global client settings with non-destructive live appearance preview."""

    preview_changed = Signal(object)

    def __init__(self, settings: ClientSettings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(560, 500)
        self._initial = settings

        appearance_box = QGroupBox("Output Appearance", self)
        appearance_form = QFormLayout(appearance_box)

        self.font_family = QFontComboBox(appearance_box)
        self.font_family.setCurrentFont(QFont(settings.font_family))
        self.font_size = QSpinBox(appearance_box)
        self.font_size.setRange(6, 72)
        self.font_size.setValue(settings.font_size)
        self.foreground = _ColorButton(settings.output_foreground, appearance_box)
        self.background = _ColorButton(settings.output_background, appearance_box)
        self.timestamps = QCheckBox("Show timestamps in the output window", appearance_box)
        self.timestamps.setChecked(settings.timestamps)

        appearance_form.addRow("Font", self.font_family)
        appearance_form.addRow("Font size", self.font_size)
        appearance_form.addRow("Default text color", self.foreground)
        appearance_form.addRow("Background color", self.background)
        appearance_form.addRow("", self.timestamps)

        behavior_box = QGroupBox("Behavior", self)
        behavior_form = QFormLayout(behavior_box)
        self.scrollback = QSpinBox(behavior_box)
        self.scrollback.setRange(100, 1_000_000)
        self.scrollback.setSingleStep(1000)
        self.scrollback.setValue(settings.scrollback_blocks)
        self.scrollback.setSuffix(" lines")
        self.local_echo = QCheckBox("Echo commands locally when the server does not", behavior_box)
        self.local_echo.setChecked(settings.local_echo)
        behavior_form.addRow("Scrollback limit", self.scrollback)
        behavior_form.addRow("", self.local_echo)

        reconnect_box = QGroupBox("New Session Reconnect Defaults", self)
        reconnect_form = QFormLayout(reconnect_box)
        self.auto_reconnect = QCheckBox("Reconnect automatically", reconnect_box)
        self.auto_reconnect.setChecked(settings.default_auto_reconnect)
        self.base_delay = QDoubleSpinBox(reconnect_box)
        self.base_delay.setRange(0.1, 3600.0)
        self.base_delay.setValue(settings.default_reconnect_base_delay)
        self.base_delay.setSuffix(" s")
        self.max_delay = QDoubleSpinBox(reconnect_box)
        self.max_delay.setRange(0.1, 3600.0)
        self.max_delay.setValue(settings.default_reconnect_max_delay)
        self.max_delay.setSuffix(" s")
        reconnect_form.addRow("", self.auto_reconnect)
        reconnect_form.addRow("Base delay", self.base_delay)
        reconnect_form.addRow("Maximum delay", self.max_delay)
        reconnect_form.addRow(
            "",
            QLabel("Connection profiles keep their own reconnect settings.", reconnect_box),
        )

        defaults = QPushButton("Restore Defaults", self)
        defaults.clicked.connect(self._restore_defaults)
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        footer = QHBoxLayout()
        footer.addWidget(defaults)
        footer.addStretch(1)
        footer.addWidget(button_box)

        layout = QVBoxLayout(self)
        layout.addWidget(appearance_box)
        layout.addWidget(behavior_box)
        layout.addWidget(reconnect_box)
        layout.addStretch(1)
        layout.addLayout(footer)

        self.font_family.currentFontChanged.connect(lambda _font: self._emit_preview())
        self.font_size.valueChanged.connect(lambda _value: self._emit_preview())
        self.foreground.color_changed.connect(lambda _value: self._emit_preview())
        self.background.color_changed.connect(lambda _value: self._emit_preview())
        self.timestamps.toggled.connect(lambda _value: self._emit_preview())
        self.scrollback.valueChanged.connect(lambda _value: self._emit_preview())
        self.local_echo.toggled.connect(lambda _value: self._emit_preview())

    def accept(self) -> None:
        try:
            self.client_settings()
        except ValueError as exc:
            QMessageBox.warning(self, "Settings", str(exc))
            return
        super().accept()

    def client_settings(self) -> ClientSettings:
        # Validation is intentionally centralized in ClientSettings.from_dict so
        # persistence, UI, and tests all share one contract.
        return ClientSettings.from_dict(
            {
                "font_family": self.font_family.currentFont().family(),
                "font_size": self.font_size.value(),
                "output_foreground": self.foreground.color,
                "output_background": self.background.color,
                "scrollback_blocks": self.scrollback.value(),
                "timestamps": self.timestamps.isChecked(),
                "local_echo": self.local_echo.isChecked(),
                "default_auto_reconnect": self.auto_reconnect.isChecked(),
                "default_reconnect_base_delay": self.base_delay.value(),
                "default_reconnect_max_delay": self.max_delay.value(),
            }
        )

    def _emit_preview(self) -> None:
        try:
            self.preview_changed.emit(self.client_settings())
        except ValueError:
            # Reconnect bounds can be momentarily inconsistent while editing;
            # appearance/behavior preview should not turn that into a dialog crash.
            return

    def _restore_defaults(self) -> None:
        defaults = ClientSettings()
        self.font_family.setCurrentFont(QFont(defaults.font_family))
        self.font_size.setValue(defaults.font_size)
        self.foreground.set_color(defaults.output_foreground)
        self.background.set_color(defaults.output_background)
        self.timestamps.setChecked(defaults.timestamps)
        self.scrollback.setValue(defaults.scrollback_blocks)
        self.local_echo.setChecked(defaults.local_echo)
        self.auto_reconnect.setChecked(defaults.default_auto_reconnect)
        self.base_delay.setValue(defaults.default_reconnect_base_delay)
        self.max_delay.setValue(defaults.default_reconnect_max_delay)
        self._emit_preview()
