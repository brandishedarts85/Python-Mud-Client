"""Dockable QMainWindow shell for the Python MUD Client."""

from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QSettings, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QFileDialog, QLabel, QMainWindow, QMessageBox, QTabWidget

from qt.dialogs.connect import ConnectDialog
from qt.dialogs.profiles import ProfilesDialog
from qt.dialogs.settings import SettingsDialog
from qt.docks.automation import AutomationDock
from qt.docks.connections import ConnectionsDock
from qt.docks.macros import MacroDock
from qt.docks.protocol import ProtocolDock
from qt.docks.variables import VariablesDock
from qt.docks.mapper import MapperDock
from qt.session_widget import MudSessionWidget
from command_pipeline import CommandSource
from client_settings import ClientSettings
from persistence import load_settings_safely, save_settings
from lifecycle import TaskOwner
from session_controller import MudSessionController
from transcript import atomic_export_text


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("MainWindow")
        self.setWindowTitle("Python MUD Client")
        self.resize(1200, 800)

        self._settings = QSettings("brandished_arts", "PythonMudClient")
        self.client_settings, self._settings_load_error = load_settings_safely()
        self._sessions: list[MudSessionWidget] = []
        self._shutdown_started = False
        self._shutdown_complete = False
        self._shutdown_task: asyncio.Task | None = None
        self._task_owner = TaskOwner("qt-main-window")

        self.setDockOptions(
            QMainWindow.DockOption.AllowNestedDocks
            | QMainWindow.DockOption.AllowTabbedDocks
            | QMainWindow.DockOption.AnimatedDocks
        )

        self.tabs = QTabWidget(self)
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.currentChanged.connect(self._active_session_changed)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.setCentralWidget(self.tabs)

        self.connections_dock = ConnectionsDock(self)
        self.automation_dock = AutomationDock(self)
        self.macro_dock = MacroDock(self)
        self.protocol_dock = ProtocolDock(self)
        self.variables_dock = VariablesDock(self)
        self.mapper_dock = MapperDock(self)

        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.connections_dock)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.automation_dock)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.protocol_dock)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.variables_dock)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.mapper_dock)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.macro_dock)
        self.tabifyDockWidget(self.automation_dock, self.protocol_dock)
        self.tabifyDockWidget(self.protocol_dock, self.variables_dock)
        self.automation_dock.raise_()

        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self._build_status_bar()

        self.connections_dock.connect_requested.connect(self.show_connect_dialog)
        self.connections_dock.disconnect_requested.connect(
            lambda: self._spawn_task(
                self.disconnect_active(), name="qt-dock-disconnect"
            )
        )
        self.connections_dock.reconnect_requested.connect(
            lambda: self._spawn_task(
                self.reconnect_active(), name="qt-dock-reconnect"
            )
        )
        self.macro_dock.command_requested.connect(self._submit_macro_command)
        self.macro_dock.enabled_changed.connect(self._macro_enabled_changed)
        self.automation_dock.automation_changed.connect(self._refresh_active_docks)

        # An application-level filter is required because QLineEdit consumes
        # keypad keypresses before they can bubble to QMainWindow.  The filter
        # is guarded to this active window and ignores modal/popup UI.
        QApplication.instance().installEventFilter(self)

        self._restore_workspace()
        self._refresh_active_docks()
        if self._settings_load_error:
            self.statusBar().showMessage(
                "Settings could not be loaded; safe defaults are active. "
                + self._settings_load_error,
                8000,
            )

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_actions(self) -> None:
        self.new_session_action = QAction("New Session", self)
        self.new_session_action.setShortcut("Ctrl+N")
        self.new_session_action.triggered.connect(lambda: self.create_session())

        self.connect_action = QAction("Connect…", self)
        self.connect_action.setShortcut("Ctrl+O")
        self.connect_action.triggered.connect(self.show_connect_dialog)

        self.disconnect_action = QAction("Disconnect", self)
        self.disconnect_action.triggered.connect(
            lambda: self._spawn_task(
                self.disconnect_active(), name="qt-mud-disconnect"
            )
        )

        self.reconnect_action = QAction("Reconnect", self)
        self.reconnect_action.triggered.connect(
            lambda: self._spawn_task(
                self.reconnect_active(), name="qt-mud-reconnect"
            )
        )

        self.profiles_action = QAction("Profiles…", self)
        self.profiles_action.triggered.connect(self._show_profiles)

        self.numpad_macros_action = QAction("Enable Numpad Macros", self)
        self.numpad_macros_action.setCheckable(True)
        self.numpad_macros_action.setChecked(self.macro_dock.enabled)
        self.numpad_macros_action.triggered.connect(self.macro_dock.set_enabled)

        self.settings_action = QAction("Settings…", self)
        self.settings_action.triggered.connect(self._show_settings)

        self.find_action = QAction("Find in Transcript…", self)
        self.find_action.setShortcut("Ctrl+F")
        self.find_action.triggered.connect(self._show_find_for_active)

        self.export_transcript_action = QAction("Export Transcript…", self)
        self.export_transcript_action.setShortcut("Ctrl+Shift+S")
        self.export_transcript_action.triggered.connect(self._export_active_transcript)

        self.start_logging_action = QAction("Start Session Logging…", self)
        self.start_logging_action.triggered.connect(self._start_active_logging)

        self.stop_logging_action = QAction("Stop Session Logging", self)
        self.stop_logging_action.triggered.connect(self._stop_active_logging)

        self.exit_action = QAction("Exit", self)
        self.exit_action.setShortcut("Ctrl+Q")
        self.exit_action.triggered.connect(self.close)

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction(self.new_session_action)
        file_menu.addSeparator()
        file_menu.addAction(self.start_logging_action)
        file_menu.addAction(self.stop_logging_action)
        file_menu.addAction(self.export_transcript_action)
        file_menu.addSeparator()
        file_menu.addAction(self.exit_action)

        edit_menu = self.menuBar().addMenu("&Edit")
        edit_menu.addAction(self.find_action)

        connection_menu = self.menuBar().addMenu("&Connection")
        connection_menu.addAction(self.connect_action)
        connection_menu.addAction(self.disconnect_action)
        connection_menu.addAction(self.reconnect_action)
        connection_menu.addSeparator()
        connection_menu.addAction(self.profiles_action)

        view_menu = self.menuBar().addMenu("&View")
        for dock in (
            self.connections_dock,
            self.automation_dock,
            self.macro_dock,
            self.protocol_dock,
            self.variables_dock,
            self.mapper_dock,
        ):
            view_menu.addAction(dock.toggleViewAction())

        tools_menu = self.menuBar().addMenu("&Tools")
        tools_menu.addAction(self.numpad_macros_action)
        tools_menu.addSeparator()
        tools_menu.addAction(self.settings_action)

    def _build_toolbar(self) -> None:
        toolbar = self.addToolBar("Main")
        toolbar.setObjectName("MainToolbar")
        toolbar.addAction(self.connect_action)
        toolbar.addAction(self.disconnect_action)
        toolbar.addAction(self.reconnect_action)

    def _build_status_bar(self) -> None:
        self.connection_status = QLabel("disconnected", self)
        self.protocol_status = QLabel("TELNET 0  |  GMCP 0  |  MSDP 0", self)
        self.terminal_status = QLabel("xterm-256color", self)

        bar = self.statusBar()
        bar.addWidget(self.connection_status, 1)
        bar.addPermanentWidget(self.protocol_status)
        bar.addPermanentWidget(self.terminal_status)

    def _spawn_task(self, awaitable, *, name: str) -> asyncio.Task:
        """Create an asyncio task owned by this window lifecycle."""
        return self._task_owner.create(awaitable, name=name)

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def create_session(
        self,
        host: str | None = None,
        port: int | None = None,
        *,
        terminal_type: str = "xterm-256color",
        auto_reconnect: bool | None = None,
        reconnect_base_delay: float | None = None,
        reconnect_max_delay: float | None = None,
        profile_name: str | None = None,
    ) -> MudSessionWidget:
        settings = self.client_settings
        controller = MudSessionController(
            host=host,
            port=port,
            scrollback_lines=settings.scrollback_blocks,
            terminal_type=terminal_type,
            auto_reconnect=(
                settings.default_auto_reconnect
                if auto_reconnect is None
                else auto_reconnect
            ),
            reconnect_base_delay=(
                settings.default_reconnect_base_delay
                if reconnect_base_delay is None
                else reconnect_base_delay
            ),
            reconnect_max_delay=(
                settings.default_reconnect_max_delay
                if reconnect_max_delay is None
                else reconnect_max_delay
            ),
            local_echo_enabled=settings.local_echo,
        )
        if profile_name:
            controller.set_automation_scope(profile_name)
        widget = MudSessionWidget(controller, self)
        widget.apply_settings(self.client_settings)
        self._sessions.append(widget)
        index = self.tabs.addTab(widget, controller.display_name)
        self.tabs.setCurrentIndex(index)

        widget.bridge.state_changed.connect(
            lambda state, session=widget: self._session_state_changed(session, state)
        )
        widget.bridge.gmcp_changed.connect(lambda _data: self._refresh_active_docks())
        widget.bridge.msdp_changed.connect(lambda _data: self._refresh_active_docks())
        widget.bridge.telnet_changed.connect(lambda _data: self._refresh_active_docks())
        widget.bridge.automation_changed.connect(self._refresh_active_docks)
        widget.output.trigger_capture_requested.connect(
            lambda text, style, session=widget: self._capture_trigger_from_output(
                session, text, style
            )
        )
        widget.logging_error.connect(
            lambda error, session=widget: self._session_logging_failed(session, error)
        )

        self._spawn_task(controller.start(), name="qt-mud-session-start")
        self._refresh_active_docks()
        widget.command_input.setFocus()
        return widget

    def active_session(self) -> MudSessionWidget | None:
        widget = self.tabs.currentWidget()
        return widget if isinstance(widget, MudSessionWidget) else None

    def _active_session_changed(self, _index: int) -> None:
        self._refresh_active_docks()
        active = self.active_session()
        if active is not None:
            active.command_input.setFocus()

    def _session_state_changed(self, session: MudSessionWidget, state) -> None:
        index = self.tabs.indexOf(session)
        if index >= 0:
            self.tabs.setTabText(index, session.controller.display_name)
        if session is self.active_session():
            self._refresh_active_docks()

    def _refresh_active_docks(self) -> None:
        active = self.active_session()
        if active is None:
            self.connections_dock.set_controller(None)
            self.automation_dock.set_controller(None)
            self.macro_dock.set_scope(None)
            self.protocol_dock.set_protocol_state(None)
            self.variables_dock.set_controller(None)
            self.mapper_dock.set_controller(None)
            self.connection_status.setText("no session")
            self.protocol_status.setText("TELNET 0  |  GMCP 0  |  MSDP 0")
            self.terminal_status.setText("—")
            self._update_connection_actions(None)
            self.find_action.setEnabled(False)
            self.export_transcript_action.setEnabled(False)
            self.start_logging_action.setEnabled(False)
            self.stop_logging_action.setEnabled(False)
            return

        controller = active.controller
        protocol = controller.protocol
        self.connections_dock.set_controller(controller)
        self.automation_dock.set_controller(controller)
        self.macro_dock.set_scope(controller.profile_name)
        self.protocol_dock.set_protocol_state(protocol)
        self.variables_dock.set_controller(controller)
        self.mapper_dock.set_controller(controller)
        self.connection_status.setText(controller.state.status_text)
        self.protocol_status.setText(
            f"TELNET {len(protocol.telnet_options)}  |  "
            f"GMCP {len(protocol.gmcp)}  |  MSDP {len(protocol.msdp)}"
        )
        self.terminal_status.setText(controller.terminal_type)
        self._update_connection_actions(controller.state)
        self.find_action.setEnabled(True)
        self.export_transcript_action.setEnabled(True)
        self.start_logging_action.setEnabled(not active.logging_active)
        self.stop_logging_action.setEnabled(active.logging_active)

    def _update_connection_actions(self, state) -> None:
        if state is None:
            self.connect_action.setEnabled(True)
            self.disconnect_action.setEnabled(False)
            self.reconnect_action.setEnabled(False)
            return
        self.connect_action.setEnabled(not state.connecting)
        self.disconnect_action.setEnabled(
            state.connected or state.connecting or state.reconnecting
        )
        self.reconnect_action.setEnabled(bool(state.host and state.port is not None))

    def _close_tab(self, index: int) -> None:
        widget = self.tabs.widget(index)
        if not isinstance(widget, MudSessionWidget):
            return
        self.tabs.removeTab(index)
        with suppress(ValueError):
            self._sessions.remove(widget)
        self._spawn_task(
            self._close_session_widget(widget),
            name="qt-mud-session-close",
        )
        if not self._sessions:
            self.create_session()

    async def _close_session_widget(self, widget: MudSessionWidget) -> None:
        """Retire the controller before destroying its Qt bridge/widgets.

        deleteLater() used to run immediately after scheduling close(), leaving
        a race where final session signals could target a QObject already
        queued for destruction.
        """
        try:
            await widget.controller.close()
        finally:
            widget.dispose()
            widget.deleteLater()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def show_connect_dialog(self) -> None:
        active = self.active_session()
        if active is None:
            active = self.create_session()
        state = active.controller.state
        dialog = ConnectDialog(
            state.host or "",
            state.port or 4000,
            active.controller.terminal_type,
            active.controller.auto_reconnect,
            active.controller.reconnect_base_delay,
            active.controller.reconnect_max_delay,
            self,
        )
        if dialog.exec() != ConnectDialog.DialogCode.Accepted:
            return
        settings = dialog.connection_settings()
        host, port = settings["host"], settings["port"]
        if not host:
            QMessageBox.warning(self, "Connect", "Please enter a host name.")
            return
        if settings["reconnect_max_delay"] < settings["reconnect_base_delay"]:
            QMessageBox.warning(
                self, "Connect", "Maximum reconnect delay must be at least the base delay."
            )
            return
        active.controller.terminal_type = settings["terminal_type"]
        active.controller.auto_reconnect = settings["auto_reconnect"]
        active.controller.reconnect_base_delay = settings["reconnect_base_delay"]
        active.controller.reconnect_max_delay = settings["reconnect_max_delay"]
        self._spawn_task(
            active.controller.connect(host, port),
            name="qt-mud-connect",
        )

    async def disconnect_active(self) -> None:
        active = self.active_session()
        if active is not None:
            await active.controller.disconnect(manual=True)

    async def reconnect_active(self) -> None:
        active = self.active_session()
        if active is not None:
            await active.controller.reconnect()

    def _session_logging_failed(self, session: MudSessionWidget, error: str) -> None:
        if session is self.active_session():
            self._refresh_active_docks()
        self.statusBar().showMessage(f"Session logging stopped: {error}", 8000)

    def _show_find_for_active(self) -> None:
        active = self.active_session()
        if active is not None:
            active.show_find_bar()

    @staticmethod
    def _safe_log_stem(value: str) -> str:
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
        return (stem or "session")[:80]

    def _default_log_path(self, session: MudSessionWidget) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        stem = self._safe_log_stem(session.controller.display_name)
        return Path("logs") / f"{stamp}-{stem}.log"

    def _start_active_logging(self) -> None:
        active = self.active_session()
        if active is None:
            return
        suggested = str(self._default_log_path(active))
        path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Start Session Logging",
            suggested,
            "Log files (*.log *.txt);;All files (*)",
        )
        if not path:
            return
        try:
            active.start_logging(path)
        except OSError as exc:
            QMessageBox.warning(self, "Session Logging", f"Could not start logging:\n{exc}")
            return
        self._refresh_active_docks()
        self.statusBar().showMessage(f"Logging to {path}", 3000)

    def _stop_active_logging(self) -> None:
        active = self.active_session()
        if active is None or not active.logging_active:
            return
        path = active.log_path
        active.stop_logging()
        self._refresh_active_docks()
        self.statusBar().showMessage(
            f"Session logging stopped{f' ({path})' if path else ''}", 3000
        )

    def _export_active_transcript(self) -> None:
        active = self.active_session()
        if active is None:
            return
        stem = self._safe_log_stem(active.controller.display_name)
        suggested = f"{stem}-transcript.txt"
        path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Export Transcript",
            suggested,
            "Text files (*.txt);;All files (*)",
        )
        if not path:
            return
        try:
            atomic_export_text(path, active.output.toPlainText())
        except OSError as exc:
            QMessageBox.warning(self, "Export Transcript", f"Could not export transcript:\n{exc}")
            return
        self.statusBar().showMessage(f"Transcript exported to {path}", 3000)

    def _show_profiles(self) -> None:
        dialog = ProfilesDialog(self)
        dialog.profile_selected.connect(self._apply_profile_to_active_session)
        dialog.exec()

    def _apply_profile_to_active_session(self, name: str, profile: dict) -> None:
        active = self.active_session()
        if active is None:
            active = self.create_session()
        controller = active.controller
        try:
            controller.set_automation_scope(name)
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Profile Automation",
                f"Could not load automation for profile {name!r}:\n{exc}",
            )
            return
        self.macro_dock.set_scope(name)
        controller.terminal_type = profile["terminal_type"]
        controller.auto_reconnect = profile["auto_reconnect"]
        controller.reconnect_base_delay = profile["reconnect_base_delay"]
        controller.reconnect_max_delay = profile["reconnect_max_delay"]
        self._spawn_task(
            controller.connect(profile["host"], profile["port"]),
            name="qt-mud-profile-connect",
        )

    def _show_settings(self) -> None:
        original = self.client_settings
        dialog = SettingsDialog(original, self)
        dialog.preview_changed.connect(self._preview_client_settings)
        result = dialog.exec()

        if result != SettingsDialog.DialogCode.Accepted:
            self._apply_client_settings(original)
            return

        try:
            chosen = dialog.client_settings()
            save_settings(chosen)
        except (ValueError, OSError) as exc:
            self._apply_client_settings(original)
            QMessageBox.warning(
                self,
                "Settings",
                "Could not save settings. The previous settings remain active.\n\n"
                + str(exc),
            )
            return

        self.client_settings = chosen
        self._apply_client_settings(chosen)
        self.statusBar().showMessage("Settings saved", 2000)

    def _preview_client_settings(self, settings: ClientSettings) -> None:
        # Only reversible appearance changes are previewed. Lowering a
        # scrollback bound can permanently trim retained lines; timestamps and
        # local echo can also affect data arriving while this modal dialog is
        # open. Those settings are therefore committed only after OK.
        current = self.client_settings
        preview = ClientSettings.from_dict(
            {
                **settings.to_dict(),
                "scrollback_blocks": current.scrollback_blocks,
                "timestamps": current.timestamps,
                "local_echo": current.local_echo,
                "default_auto_reconnect": current.default_auto_reconnect,
                "default_reconnect_base_delay": current.default_reconnect_base_delay,
                "default_reconnect_max_delay": current.default_reconnect_max_delay,
            }
        )
        self._apply_client_settings(preview)

    def _apply_client_settings(self, settings: ClientSettings) -> None:
        for session in tuple(self._sessions):
            session.apply_settings(settings)

    def _submit_macro_command(self, command: str) -> None:
        active = self.active_session()
        if active is None:
            self.statusBar().showMessage("No active session for macro command", 2500)
            return
        active.controller.submit_command(command, source=CommandSource.MACRO)
        active.command_input.setFocus()

    def _macro_enabled_changed(self, enabled: bool) -> None:
        self.numpad_macros_action.setChecked(enabled)
        state = "enabled" if enabled else "disabled"
        self.statusBar().showMessage(f"Numpad macros {state}", 2000)

    def _capture_trigger_from_output(self, session, text: str, style) -> None:
        index = self.tabs.indexOf(session)
        if index >= 0 and self.tabs.currentIndex() != index:
            self.tabs.setCurrentIndex(index)
        self.automation_dock.set_controller(session.controller)
        self.automation_dock.show()
        self.automation_dock.raise_()
        self.automation_dock.capture_trigger(text, style)

    def eventFilter(self, watched, event):  # noqa: N802 - Qt API
        if (
            event.type() == QEvent.Type.KeyPress
            and self.isActiveWindow()
            and QApplication.activeModalWidget() is None
            and QApplication.activePopupWidget() is None
        ):
            if self.macro_dock.handle_key_event(event):
                return True
        return super().eventFilter(watched, event)

    # ------------------------------------------------------------------
    # Persistent workspace
    # ------------------------------------------------------------------

    def _restore_workspace(self) -> None:
        geometry = self._settings.value("window/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        state = self._settings.value("window/state")
        if state is not None:
            self.restoreState(state)

    def _save_workspace(self) -> None:
        self._settings.setValue("window/geometry", self.saveGeometry())
        self._settings.setValue("window/state", self.saveState())

    async def shutdown(self) -> None:
        """Close all sessions and their background tasks exactly once."""
        if self._shutdown_complete:
            return

        self._shutdown_started = True
        self._save_workspace()

        # Stop in-flight UI operations (connect/reconnect/tab-close) before
        # asking controllers to reach their terminal state.
        await self._task_owner.cancel_and_wait()

        await asyncio.gather(
            *(session.controller.close() for session in tuple(self._sessions)),
            return_exceptions=True,
        )
        self.variables_dock.dispose()
        self._shutdown_complete = True

    async def _shutdown_then_close(self) -> None:
        try:
            await self.shutdown()
        finally:
            # Re-enter closeEvent after cleanup.  _shutdown_complete makes the
            # second pass synchronous, allowing Qt to close the final window
            # and emit lastWindowClosed/aboutToQuit normally.
            self.close()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self._shutdown_complete:
            event.accept()
            return

        event.ignore()
        if self._shutdown_task is None or self._shutdown_task.done():
            self._shutdown_task = asyncio.create_task(
                self._shutdown_then_close(),
                name="qt-mud-application-shutdown",
            )

