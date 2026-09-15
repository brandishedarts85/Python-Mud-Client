from __future__ import annotations

import re
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QDockWidget,
)

from ansi_parser import Style
from automation import TriggerStyleFilter


_ANSI_PAIRS = (
    ((0, 0, 0), (127, 127, 127)),
    ((205, 0, 0), (255, 0, 0)),
    ((0, 205, 0), (0, 255, 0)),
    ((205, 205, 0), (255, 255, 0)),
    ((0, 0, 238), (92, 92, 255)),
    ((205, 0, 205), (255, 0, 255)),
    ((0, 205, 205), (0, 255, 255)),
    ((229, 229, 229), (255, 255, 255)),
)


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


class _ColorList(QWidget):
    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        self.list = QListWidget(self)
        self.list.setMaximumHeight(82)
        self.add_button = QPushButton("Add…", self)
        self.remove_button = QPushButton("Remove", self)
        self.pair_button = QPushButton("Add ANSI dark/bright pair", self)
        self.pair_button.setToolTip(
            "When a captured color is one of the classic ANSI colors, add both "
            "its normal and bright variants."
        )

        buttons = QHBoxLayout()
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.remove_button)
        buttons.addWidget(self.pair_button)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel(title, self))
        layout.addWidget(self.list)
        layout.addLayout(buttons)

        self.add_button.clicked.connect(self._pick_color)
        self.remove_button.clicked.connect(self._remove_selected)
        self.pair_button.clicked.connect(self._add_pair_for_selected)

    def colors(self) -> tuple[tuple[int, int, int], ...]:
        values: list[tuple[int, int, int]] = []
        for row in range(self.list.count()):
            value = self.list.item(row).data(Qt.ItemDataRole.UserRole)
            if value is not None:
                values.append(tuple(value))
        return tuple(values)

    def add_color(self, rgb: tuple[int, int, int] | None) -> None:
        if rgb is None:
            return
        rgb = tuple(int(v) for v in rgb)
        if rgb in self.colors():
            return
        item = QListWidgetItem(_hex(rgb))
        item.setData(Qt.ItemDataRole.UserRole, rgb)
        item.setForeground(QColor(*rgb))
        self.list.addItem(item)
        self.list.setCurrentItem(item)

    def _pick_color(self) -> None:
        color = QColorDialog.getColor(QColor(), self, "Select trigger color")
        if color.isValid():
            self.add_color((color.red(), color.green(), color.blue()))

    def _remove_selected(self) -> None:
        row = self.list.currentRow()
        if row >= 0:
            self.list.takeItem(row)

    def _add_pair_for_selected(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        selected = tuple(self.list.item(row).data(Qt.ItemDataRole.UserRole))
        for dark, bright in _ANSI_PAIRS:
            if selected in (dark, bright):
                self.add_color(dark)
                self.add_color(bright)
                return
        QMessageBox.information(
            self,
            "ANSI Color Pair",
            "The selected color is not an exact classic ANSI 16-color value. "
            "You can still add any additional color with Add….",
        )


class _AliasDialog(QDialog):
    def __init__(self, pattern="", expansion="", enabled=True, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Alias")
        self.pattern = QLineEdit(pattern, self)
        self.expansion = QLineEdit(expansion, self)
        self.enabled = QCheckBox("Enabled", self)
        self.enabled.setChecked(enabled)
        form = QFormLayout()
        form.addRow("Pattern", self.pattern)
        form.addRow("Expansion", self.expansion)
        form.addRow("", self.enabled)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)


class _TriggerDialog(QDialog):
    def __init__(
        self,
        trigger=None,
        *,
        capture_text: str | None = None,
        capture_style: Style | tuple[Style, ...] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Trigger")

        # Keep the editor usable on laptop-height displays.  The content body
        # scrolls while the OK/Cancel footer remains permanently reachable.
        screen = parent.screen() if parent is not None and parent.screen() else QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            width = min(680, max(520, int(available.width() * 0.80)))
            height = min(720, max(420, int(available.height() * 0.82)))
            self.resize(width, height)
            self.setMaximumSize(
                max(520, int(available.width() * 0.95)),
                max(420, int(available.height() * 0.95)),
            )
        else:
            self.resize(620, 620)

        pattern = trigger.pattern if trigger else ""
        if capture_text is not None:
            pattern = re.escape(capture_text)
        self.pattern = QLineEdit(pattern, self)
        self.response = QLineEdit(trigger.response_template if trigger else "", self)
        self.enabled = QCheckBox("Enabled", self)
        self.enabled.setChecked(trigger.enabled if trigger else True)
        self.gag = QCheckBox("Gag matching line", self)
        self.gag.setChecked(trigger.gag if trigger else False)
        self.oneshot = QCheckBox("One shot", self)
        self.oneshot.setChecked(trigger.one_shot if trigger else False)
        self.case = QCheckBox("Case sensitive", self)
        self.case.setChecked(trigger.case_sensitive if trigger else False)
        self.cooldown = QDoubleSpinBox(self)
        self.cooldown.setRange(0, 86400)
        self.cooldown.setValue(trigger.cooldown_s if trigger else 0)
        self.cooldown.setSuffix(" s")
        self.priority = QSpinBox(self)
        self.priority.setRange(-1000, 1000)
        self.priority.setValue(trigger.priority if trigger else 0)
        self.priority.setToolTip("Higher-priority triggers are evaluated first.")

        form = QFormLayout()
        form.addRow("Regex pattern", self.pattern)
        form.addRow("Response", self.response)
        form.addRow("Cooldown", self.cooldown)
        form.addRow("Priority", self.priority)
        form.addRow("", self.enabled)
        form.addRow("", self.gag)
        form.addRow("", self.oneshot)
        form.addRow("", self.case)

        style_group = QGroupBox("ANSI / color match", self)
        style_layout = QVBoxLayout(style_group)
        self.style_enabled = QCheckBox(
            "Require the regex match to use the selected presentation style", style_group
        )
        style_layout.addWidget(self.style_enabled)
        self.fg_colors = _ColorList("Allowed foreground colors", style_group)
        self.allow_default_fg = QCheckBox("Allow default/unset foreground", style_group)
        self.bg_colors = _ColorList("Allowed background colors", style_group)
        self.allow_default_bg = QCheckBox("Allow default/unset background", style_group)
        style_layout.addWidget(self.fg_colors)
        style_layout.addWidget(self.allow_default_fg)
        style_layout.addWidget(self.bg_colors)
        style_layout.addWidget(self.allow_default_bg)

        attr_form = QFormLayout()
        self.attr_combos: dict[str, QComboBox] = {}
        for name, label in (
            ("bold", "Bold"),
            ("dim", "Dim"),
            ("italic", "Italic"),
            ("underline", "Underline"),
            ("blink", "Blink"),
            ("strike", "Strike"),
        ):
            combo = QComboBox(style_group)
            combo.addItem("Ignore", None)
            combo.addItem("Required on", True)
            combo.addItem("Required off", False)
            self.attr_combos[name] = combo
            attr_form.addRow(label, combo)
        style_layout.addLayout(attr_form)
        style_layout.addWidget(
            QLabel(
                "Color checks apply to every non-whitespace character matched by the regex. "
                "This prevents the same words in differently-colored chat from firing the trigger.",
                style_group,
            )
        )

        existing_filter = trigger.style_filter if trigger else None
        if existing_filter is not None and existing_filter.active:
            self.style_enabled.setChecked(True)
            for color in existing_filter.foregrounds:
                self.fg_colors.add_color(color)
            for color in existing_filter.backgrounds:
                self.bg_colors.add_color(color)
            self.allow_default_fg.setChecked(existing_filter.allow_default_foreground)
            self.allow_default_bg.setChecked(existing_filter.allow_default_background)
            for name, combo in self.attr_combos.items():
                value = getattr(existing_filter, name)
                combo.setCurrentIndex(0 if value is None else (1 if value else 2))
        elif capture_style is not None:
            captured_styles = (
                capture_style
                if isinstance(capture_style, tuple)
                else (capture_style,)
            )
            self.style_enabled.setChecked(True)
            for style in captured_styles:
                if style.fg is None:
                    self.allow_default_fg.setChecked(True)
                else:
                    self.fg_colors.add_color(style.fg)
                if style.bg is None:
                    self.allow_default_bg.setChecked(True)
                else:
                    self.bg_colors.add_color(style.bg)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        # Put the tall form inside a scroll area; keep dialog buttons outside
        # it so Save/Cancel never disappear below the bottom of the screen.
        content = QWidget(self)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.addLayout(form)
        content_layout.addWidget(style_group)
        content_layout.addStretch(1)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(content)

        layout = QVBoxLayout(self)
        layout.addWidget(scroll, 1)
        layout.addWidget(buttons, 0)

    def style_filter(self) -> TriggerStyleFilter | None:
        if not self.style_enabled.isChecked():
            return None
        values = {
            name: combo.currentData()
            for name, combo in self.attr_combos.items()
        }
        result = TriggerStyleFilter(
            foregrounds=self.fg_colors.colors(),
            backgrounds=self.bg_colors.colors(),
            allow_default_foreground=self.allow_default_fg.isChecked(),
            allow_default_background=self.allow_default_bg.isChecked(),
            **values,
        )
        return result if result.active else None


class _TriggerTestDialog(QDialog):
    def __init__(self, controller, trigger_id: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Test Trigger")
        self.resize(700, 420)
        self.controller = controller
        self.trigger_id = trigger_id
        self.latest_line = controller.last_styled_line

        trigger = controller.engine.triggers[trigger_id]
        self.pattern_label = QLabel(f"Pattern: {trigger.pattern}", self)
        self.text = QPlainTextEdit(self)
        if self.latest_line is not None:
            self.text.setPlainText(self.latest_line.plain_text())
        self.use_latest_style = QCheckBox(
            "Use parsed ANSI/style from the latest received line when the text is unchanged",
            self,
        )
        self.use_latest_style.setChecked(self.latest_line is not None)
        self.result = QLabel("Press Test to evaluate without firing the trigger.", self)
        self.result.setWordWrap(True)

        self.test_button = QPushButton("Test", self)
        self.close_button = QPushButton("Close", self)
        self.test_button.clicked.connect(self._run_test)
        self.close_button.clicked.connect(self.accept)

        row = QHBoxLayout()
        row.addWidget(self.test_button)
        row.addStretch(1)
        row.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.pattern_label)
        layout.addWidget(QLabel("Candidate line", self))
        layout.addWidget(self.text)
        layout.addWidget(self.use_latest_style)
        layout.addWidget(self.result)
        layout.addLayout(row)

    def _run_test(self) -> None:
        text = self.text.toPlainText()
        styled = None
        if (
            self.use_latest_style.isChecked()
            and self.latest_line is not None
            and text == self.latest_line.plain_text()
        ):
            styled = self.latest_line
        result = self.controller.engine.explain_trigger(
            self.trigger_id, text, styled_line=styled
        )
        style_status = "not required"
        if result.style_required:
            if not result.style_available:
                style_status = "unavailable"
            else:
                style_status = "MATCH" if result.style_matched else "NO MATCH"
        lines = [
            f"Master automation: {'ON' if result.master_enabled else 'OFF'}",
            f"Trigger enabled: {'YES' if result.trigger_enabled else 'NO'}",
            f"Regex: {'MATCH' if result.regex_matched else 'NO MATCH'}",
            f"ANSI/style: {style_status}",
            f"Cooldown: {'READY' if result.cooldown_ready else 'WAITING'}",
            f"Would fire: {'YES' if result.would_fire else 'NO'}",
            f"Reason: {result.reason}",
        ]
        if result.matched_text:
            lines.insert(3, f"Matched text: {result.matched_text!r}")
        self.result.setText("\n".join(lines))


class AutomationDock(QDockWidget):
    automation_changed = Signal()

    def __init__(self, parent=None):
        super().__init__("Automation", parent)
        self.setObjectName("AutomationDock")
        self._controller = None
        self._automation_subscription = None

        body = QWidget(self)
        root = QVBoxLayout(body)

        top = QHBoxLayout()
        self.master_enabled = QCheckBox("Enable automation", body)
        self.master_enabled.setToolTip(
            "Master switch for aliases, triggers, and timers. Use Save to persist this state."
        )
        self.scope_label = QLabel("Scope: Global", body)
        top.addWidget(self.master_enabled)
        top.addStretch(1)
        top.addWidget(self.scope_label)
        root.addLayout(top)

        self.tabs = QTabWidget(body)

        self.alias_table = QTableWidget(0, 3, body)
        self.alias_table.setHorizontalHeaderLabels(["On", "Pattern", "Expansion"])
        self.alias_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.alias_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        self.trigger_table = QTableWidget(0, 8, body)
        self.trigger_table.setHorizontalHeaderLabels(
            ["On", "Pattern", "Response", "Priority", "Style", "Gag", "One-shot", "Cooldown"]
        )
        self.trigger_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.trigger_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        self.fire_table = QTableWidget(0, 4, body)
        self.fire_table.setHorizontalHeaderLabels(["Time", "Pattern", "Matched", "Response"] )
        self.fire_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        self.tabs.addTab(self.alias_table, "Aliases")
        self.tabs.addTab(self.trigger_table, "Triggers")
        self.tabs.addTab(self.fire_table, "Firing Log")
        root.addWidget(self.tabs)

        row = QHBoxLayout()
        self.add_btn = QPushButton("Add", body)
        self.edit_btn = QPushButton("Edit", body)
        self.delete_btn = QPushButton("Delete", body)
        self.toggle_btn = QPushButton("Enable/Disable", body)
        self.test_btn = QPushButton("Test", body)
        self.clear_log_btn = QPushButton("Clear Log", body)
        self.save_btn = QPushButton("Save", body)
        for button in (
            self.add_btn, self.edit_btn, self.delete_btn, self.toggle_btn,
            self.test_btn, self.clear_log_btn, self.save_btn
        ):
            row.addWidget(button)
        root.addLayout(row)
        self.setWidget(body)

        self.add_btn.clicked.connect(self._add)
        self.edit_btn.clicked.connect(self._edit)
        self.delete_btn.clicked.connect(self._delete)
        self.toggle_btn.clicked.connect(self._toggle)
        self.test_btn.clicked.connect(self._test_selected_trigger)
        self.clear_log_btn.clicked.connect(self._clear_log)
        self.save_btn.clicked.connect(self._save)
        self.master_enabled.toggled.connect(self._master_toggled)
        self.alias_table.doubleClicked.connect(lambda _i: self._edit())
        self.trigger_table.doubleClicked.connect(lambda _i: self._edit())

    def set_controller(self, controller):
        if self._controller is controller:
            self.refresh()
            return
        if self._automation_subscription is not None:
            self._automation_subscription.close()
            self._automation_subscription = None
        self._controller = controller
        if controller is not None:
            self._automation_subscription = controller.on(
                "automation_fire", self._automation_fired
            )
        self.refresh()

    def set_engine(self, engine):
        if self._controller is not None and (
            engine is not None and self._controller.engine is not engine
        ):
            # Legacy engine-only callers must not leave this dock subscribed to
            # a controller it no longer represents.
            self.set_controller(None)
        self.refresh(engine)

    @staticmethod
    def _style_summary(trigger) -> str:
        filt = trigger.style_filter
        if filt is None or not filt.active:
            return "Any"
        parts: list[str] = []
        if filt.foregrounds:
            fg = "/".join(_hex(c) for c in filt.foregrounds)
            if filt.allow_default_foreground:
                fg += "/Default"
            parts.append("FG " + fg)
        elif filt.allow_default_foreground:
            parts.append("FG Default")
        if filt.backgrounds:
            bg = "/".join(_hex(c) for c in filt.backgrounds)
            if filt.allow_default_background:
                bg += "/Default"
            parts.append("BG " + bg)
        elif filt.allow_default_background:
            parts.append("BG Default")
        for name in ("bold", "dim", "italic", "underline", "blink", "strike"):
            value = getattr(filt, name)
            if value is not None:
                parts.append(("+" if value else "-") + name)
        return ", ".join(parts) or "Any"

    def refresh(self, engine=None):
        engine = engine or (self._controller.engine if self._controller else None)
        self.alias_table.setRowCount(0)
        self.trigger_table.setRowCount(0)
        self.scope_label.setText(
            f"Scope: {self._controller.automation_scope_label}" if self._controller else "Scope: —"
        )
        self.master_enabled.blockSignals(True)
        self.master_enabled.setChecked(bool(engine.enabled) if engine is not None else False)
        self.master_enabled.setEnabled(engine is not None)
        self.master_enabled.blockSignals(False)
        if engine is None:
            self.refresh_fire_log()
            return

        for id_, alias in engine.aliases.items():
            if not isinstance(alias.expansion, str):
                continue
            row = self.alias_table.rowCount()
            self.alias_table.insertRow(row)
            values = ["✓" if alias.enabled else "", alias.pattern, alias.expansion]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, id_)
                self.alias_table.setItem(row, column, item)

        for id_, trigger in engine.triggers.items():
            if trigger.response_template is None:
                continue
            row = self.trigger_table.rowCount()
            self.trigger_table.insertRow(row)
            values = [
                "✓" if trigger.enabled else "",
                trigger.pattern,
                trigger.response_template,
                str(trigger.priority),
                self._style_summary(trigger),
                "✓" if trigger.gag else "",
                "✓" if trigger.one_shot else "",
                f"{trigger.cooldown_s:g}s",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, id_)
                self.trigger_table.setItem(row, column, item)

        self.alias_table.resizeColumnsToContents()
        self.trigger_table.resizeColumnsToContents()
        self.refresh_fire_log()

    def _selection(self):
        if self.tabs.currentIndex() == 2:
            return None, None
        table = self.alias_table if self.tabs.currentIndex() == 0 else self.trigger_table
        row = table.currentRow()
        if row < 0:
            return None, None
        return (
            "alias" if table is self.alias_table else "trigger",
            table.item(row, 0).data(Qt.ItemDataRole.UserRole),
        )

    def _changed(self):
        self.refresh()
        self.automation_changed.emit()

    def _add(self):
        if not self._controller:
            return
        if self.tabs.currentIndex() == 0:
            dialog = _AliasDialog(parent=self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            if not dialog.pattern.text().strip():
                QMessageBox.warning(self, "Alias", "Pattern is required.")
                return
            id_ = self._controller.engine.add_alias(
                dialog.pattern.text().strip(), dialog.expansion.text()
            )
            self._controller.engine.set_enabled("alias", id_, dialog.enabled.isChecked())
        else:
            self._create_trigger_dialog()
            return
        self._changed()

    def _create_trigger_dialog(
        self,
        *,
        capture_text: str | None = None,
        capture_style: Style | tuple[Style, ...] | None = None,
    ) -> None:
        if not self._controller:
            return
        dialog = _TriggerDialog(
            capture_text=capture_text,
            capture_style=capture_style,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            id_ = self._controller.engine.add_simple_trigger(
                dialog.pattern.text(),
                dialog.response.text(),
                gag=dialog.gag.isChecked(),
                one_shot=dialog.oneshot.isChecked(),
                cooldown_s=dialog.cooldown.value(),
                case_sensitive=dialog.case.isChecked(),
                priority=dialog.priority.value(),
                style_filter=dialog.style_filter(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "Trigger", str(exc))
            return
        self._controller.engine.set_enabled("trigger", id_, dialog.enabled.isChecked())
        self.tabs.setCurrentIndex(1)
        self._changed()

    def capture_trigger(
        self, text: str, style: Style | tuple[Style, ...] | None
    ) -> None:
        """Open a trigger editor prefilled from selected rendered MUD output."""
        if not self._controller:
            return
        self._create_trigger_dialog(capture_text=text, capture_style=style)

    def _edit(self):
        if not self._controller:
            return
        kind, id_ = self._selection()
        if not id_:
            return
        engine = self._controller.engine
        if kind == "alias":
            old = engine.aliases[id_]
            dialog = _AliasDialog(old.pattern, old.expansion, old.enabled, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            if not dialog.pattern.text().strip():
                QMessageBox.warning(self, "Alias", "Pattern is required.")
                return
            new = engine.add_alias(
                dialog.pattern.text().strip(), dialog.expansion.text(), alias_id=id_
            )
            engine.set_enabled("alias", new, dialog.enabled.isChecked())
        else:
            old = engine.triggers[id_]
            dialog = _TriggerDialog(old, parent=self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            try:
                new = engine.add_simple_trigger(
                    dialog.pattern.text(),
                    dialog.response.text(),
                    gag=dialog.gag.isChecked(),
                    one_shot=dialog.oneshot.isChecked(),
                    cooldown_s=dialog.cooldown.value(),
                    case_sensitive=dialog.case.isChecked(),
                    priority=dialog.priority.value(),
                    style_filter=dialog.style_filter(),
                    trigger_id=id_,
                )
                engine.set_enabled("trigger", new, dialog.enabled.isChecked())
            except Exception as exc:
                QMessageBox.warning(self, "Trigger", str(exc))
                return
        self._changed()

    def _delete(self):
        if not self._controller:
            return
        kind, id_ = self._selection()
        if id_ and QMessageBox.question(
            self, "Automation", f"Delete selected {kind}?"
        ) == QMessageBox.StandardButton.Yes:
            self._controller.engine.remove(kind, id_)
            self._changed()

    def _toggle(self):
        if not self._controller:
            return
        kind, id_ = self._selection()
        if not id_:
            return
        item = self._controller.engine._store_for_kind(kind).get(id_)
        self._controller.engine.set_enabled(kind, id_, not item.enabled)
        self._changed()

    def _master_toggled(self, enabled: bool) -> None:
        if not self._controller:
            return
        self._controller.set_automation_enabled(enabled)
        self.automation_changed.emit()

    def _automation_fired(self, _event) -> None:
        self.refresh_fire_log()

    def refresh_fire_log(self) -> None:
        self.fire_table.setRowCount(0)
        if not self._controller:
            return
        for event in reversed(self._controller.automation_fire_log):
            row = self.fire_table.rowCount()
            self.fire_table.insertRow(row)
            values = [
                datetime.fromtimestamp(event.fired_at).strftime("%H:%M:%S"),
                event.pattern,
                event.matched_text,
                event.response or "(code action)",
            ]
            for column, value in enumerate(values):
                self.fire_table.setItem(row, column, QTableWidgetItem(str(value)))
        self.fire_table.resizeColumnsToContents()

    def _clear_log(self) -> None:
        if self._controller:
            self._controller.automation_fire_log.clear()
        self.refresh_fire_log()

    def _test_selected_trigger(self) -> None:
        if not self._controller:
            return
        kind, id_ = self._selection()
        if kind != "trigger" or not id_:
            QMessageBox.information(self, "Test Trigger", "Select a trigger first.")
            return
        _TriggerTestDialog(self._controller, id_, self).exec()

    def _save(self):
        if not self._controller:
            return
        try:
            self._controller.save_automation_now()
        except Exception as exc:
            QMessageBox.warning(self, "Automation", f"Could not save automation:\n{exc}")
            return
        QMessageBox.information(
            self,
            "Automation",
            f"Automation saved for scope: {self._controller.automation_scope_label}.",
        )
