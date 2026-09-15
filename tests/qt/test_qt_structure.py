from pathlib import Path


def test_expected_qt_modules_exist():
    root = Path(__file__).resolve().parents[2]
    expected = [
        "qt/app.py",
        "qt/main_window.py",
        "qt/session_widget.py",
        "qt/session_bridge.py",
        "qt/output_view.py",
        "qt/command_input.py",
        "qt/docks/connections.py",
        "qt/docks/automation.py",
        "qt/docks/macros.py",
        "qt/docks/protocol.py",
        "qt/docks/variables.py",
        "qt/dialogs/connect.py",
    ]
    assert all((root / name).exists() for name in expected)


def test_qt_app_has_explicit_shutdown_and_sigint_paths():
    root = Path(__file__).resolve().parents[2]
    app_source = (root / "qt/app.py").read_text(encoding="utf-8")
    window_source = (root / "qt/main_window.py").read_text(encoding="utf-8")

    assert "lastWindowClosed.connect(loop.stop)" in app_source
    assert "signal.signal(signal.SIGINT" in app_source
    assert "_cancel_remaining_tasks(loop)" in app_source
    assert "async def shutdown(self)" in window_source
    assert "qt-mud-application-shutdown" in window_source


def test_output_capture_preserves_parser_style_metadata():
    root = Path(__file__).resolve().parents[2]
    source = (root / "qt/output_view.py").read_text(encoding="utf-8")
    assert "QTextFormat.Property.UserProperty" in source
    assert "fmt.setProperty(_STYLE_METADATA_PROPERTY" in source
    assert "fmt.property(_STYLE_METADATA_PROPERTY)" in source


def test_automation_dock_exposes_reliability_controls_in_source():
    root = Path(__file__).resolve().parents[2]
    source = (root / "qt/docks/automation.py").read_text(encoding="utf-8")
    assert "Enable automation" in source
    assert "Firing Log" in source
    assert "Test Trigger" in source
    assert "Priority" in source


def test_saved_profile_launch_passes_profile_name_to_session():
    root = Path(__file__).resolve().parents[2]
    app_source = (root / "qt/app.py").read_text(encoding="utf-8")
    window_source = (root / "qt/main_window.py").read_text(encoding="utf-8")
    assert 'result["profile_name"] = arg1' in app_source
    assert "controller.set_automation_scope(profile_name)" in window_source


def test_trigger_editor_is_scrollable_with_fixed_action_footer():
    root = Path(__file__).resolve().parents[2]
    source = (root / "qt/docks/automation.py").read_text(encoding="utf-8")
    assert "QScrollArea" in source
    assert "scroll.setWidgetResizable(True)" in source
    assert "layout.addWidget(scroll, 1)" in source
    assert "layout.addWidget(buttons, 0)" in source
    assert "available.height() * 0.82" in source


def test_profile_startup_uses_fail_soft_loader():
    root = Path(__file__).resolve().parents[2]
    app_source = (root / "qt" / "app.py").read_text(encoding="utf-8")
    profiles_source = (root / "qt" / "dialogs" / "profiles.py").read_text(encoding="utf-8")
    assert "load_profiles_safely" in app_source
    assert "load_profiles_safely" in profiles_source
    assert "The file was left untouched" in app_source


def test_tab_close_waits_for_controller_before_deleting_widget():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "qt" / "main_window.py").read_text(encoding="utf-8")
    helper = source[source.index("async def _close_session_widget"):]
    assert "await widget.controller.close()" in helper
    assert helper.index("await widget.controller.close()") < helper.index("widget.deleteLater()")


def test_protocol_inspector_keeps_named_telnet_table_and_events_tab():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    protocol_source = (root / "qt" / "docks" / "protocol.py").read_text(encoding="utf-8")
    main_source = (root / "qt" / "main_window.py").read_text(encoding="utf-8")

    assert 'self.tabs.addTab(self.events, "Events")' in protocol_source
    assert '24: "TTYPE"' in protocol_source
    assert '201: "GMCP"' in protocol_source
    assert 'self.protocol_status = QLabel("TELNET 0  |  GMCP 0  |  MSDP 0", self)' in main_source
    assert "_update_connection_actions" in main_source


def test_qt_bridge_and_automation_dock_have_explicit_subscription_teardown():
    root = Path(__file__).resolve().parents[2]
    bridge = (root / "qt" / "session_bridge.py").read_text(encoding="utf-8")
    dock = (root / "qt" / "docks" / "automation.py").read_text(encoding="utf-8")
    window = (root / "qt" / "main_window.py").read_text(encoding="utf-8")

    assert "self._subscriptions" in bridge
    assert "subscription.close()" in bridge
    assert "widget.dispose()" in window
    assert "self._automation_subscription.close()" in dock
    assert "self.set_controller(None)" in dock


def test_settings_feature_has_model_persistence_and_live_apply_boundaries():
    root = Path(__file__).resolve().parents[2]
    window = (root / "qt" / "main_window.py").read_text(encoding="utf-8")
    dialog = (root / "qt" / "dialogs" / "settings.py").read_text(encoding="utf-8")
    output = (root / "qt" / "output_view.py").read_text(encoding="utf-8")

    assert "load_settings_safely" in window
    assert "save_settings(chosen)" in window
    assert "preview_changed" in dialog
    assert "Restore Defaults" in dialog
    assert "def apply_appearance" in output
    assert "scrollback_blocks" in window
    assert "timestamps" in window
    assert "local_echo" in window


def test_variables_feature_has_profile_scope_ui_and_command_boundary():
    root = Path(__file__).resolve().parents[2]
    dock = (root / "qt" / "docks" / "variables.py").read_text(encoding="utf-8")
    window = (root / "qt" / "main_window.py").read_text(encoding="utf-8")
    controller = (root / "session_controller.py").read_text(encoding="utf-8")
    pipeline = (root / "command_pipeline.py").read_text(encoding="utf-8")

    assert "class VariablesDock" in dock
    assert "self.variables_dock = VariablesDock" in window
    assert "profile_variables_path" in controller
    assert 'self._emit("variables", None)' in controller
    assert "expand_variables" in pipeline
    assert '${name}' in dock
