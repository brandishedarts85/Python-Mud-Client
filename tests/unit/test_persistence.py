from automation import AutomationEngine
from persistence import delete_profile, load_automation, load_profiles, save_automation, save_profile


def test_profile_round_trip(tmp_path):
    path = tmp_path / "profiles.json"
    save_profile("local", "example.test", 4000, path=str(path))

    assert load_profiles(str(path))["local"] == {
        "host": "example.test",
        "port": 4000,
        "terminal_type": "xterm-256color",
        "auto_reconnect": True,
        "reconnect_base_delay": 3.0,
        "reconnect_max_delay": 60.0,
    }


def test_automation_round_trip(tmp_path):
    path = tmp_path / "automation.json"
    engine = AutomationEngine(send_fn=lambda _line: None)
    engine.add_alias("l", "look")
    engine.add_simple_trigger(r"^hi$", "wave", gag=True, cooldown_s=1.5)
    save_automation(engine, str(path))

    restored = AutomationEngine(send_fn=lambda _line: None)
    alias_count, trigger_count = load_automation(restored, str(path))

    assert (alias_count, trigger_count) == (1, 1)
    assert next(iter(restored.aliases.values())).expansion == "look"
    trigger = next(iter(restored.triggers.values()))
    assert trigger.response_template == "wave"
    assert trigger.gag is True
    assert trigger.cooldown_s == 1.5


def test_legacy_profile_loads_with_new_defaults(tmp_path):
    path = tmp_path / "profiles.json"
    path.write_text('{"old": {"host": "legacy.test", "port": 23}}', encoding="utf-8")
    profile = load_profiles(str(path))["old"]
    assert profile["terminal_type"] == "xterm-256color"
    assert profile["auto_reconnect"] is True
    assert profile["reconnect_base_delay"] == 3.0
    assert profile["reconnect_max_delay"] == 60.0


def test_profile_round_trip_custom_connection_settings(tmp_path):
    path = tmp_path / "profiles.json"
    save_profile(
        "custom", "mud.example", 4242, path=str(path),
        terminal_type="ansi", auto_reconnect=False,
        reconnect_base_delay=5.0, reconnect_max_delay=45.0,
    )
    assert load_profiles(str(path))["custom"] == {
        "host": "mud.example", "port": 4242, "terminal_type": "ansi",
        "auto_reconnect": False, "reconnect_base_delay": 5.0,
        "reconnect_max_delay": 45.0,
    }


def test_delete_profile(tmp_path):
    path = tmp_path / "profiles.json"
    save_profile("gone", "example.test", 4000, path=str(path))
    assert delete_profile("gone", str(path)) is True
    assert delete_profile("gone", str(path)) is False
    assert load_profiles(str(path)) == {}


def test_styled_trigger_round_trip(tmp_path):
    from automation import TriggerStyleFilter

    path = tmp_path / "automation.json"
    engine = AutomationEngine(send_fn=lambda _line: None)
    engine.add_simple_trigger(
        r"^You're DYING!!$",
        "quaff potion",
        style_filter=TriggerStyleFilter(
            foregrounds=((205, 0, 0), (255, 0, 0)),
            bold=None,
        ),
    )
    save_automation(engine, str(path))

    restored = AutomationEngine(send_fn=lambda _line: None)
    load_automation(restored, str(path))
    trigger = next(iter(restored.triggers.values()))
    assert trigger.style_filter is not None
    assert trigger.style_filter.foregrounds == ((205, 0, 0), (255, 0, 0))


def test_numpad_macro_round_trip(tmp_path):
    from persistence import load_macros, save_macros

    path = tmp_path / "macros.json"
    config = {
        "enabled": False,
        "bindings": {
            "base": {"8": {"label": "N", "command": "north", "enabled": True}},
            "ctrl": {},
            "alt": {"5": {"label": "L", "command": "look", "enabled": True}},
        },
    }
    save_macros(config, str(path))
    restored = load_macros(str(path))
    assert restored["enabled"] is False
    assert restored["bindings"]["base"]["8"]["command"] == "north"
    assert restored["bindings"]["alt"]["5"]["command"] == "look"


def test_style_filter_default_color_flags_round_trip(tmp_path):
    from automation import AutomationEngine, TriggerStyleFilter
    from persistence import load_automation, save_automation

    path = tmp_path / "automation.json"
    engine = AutomationEngine(send_fn=lambda _line: None)
    engine.add_simple_trigger(
        r"prompt",
        "n",
        style_filter=TriggerStyleFilter(
            foregrounds=((205, 205, 0),),
            allow_default_foreground=True,
            allow_default_background=True,
        ),
    )
    save_automation(engine, str(path))

    loaded = AutomationEngine(send_fn=lambda _line: None)
    load_automation(loaded, str(path))
    trigger = next(iter(loaded.triggers.values()))
    assert trigger.style_filter is not None
    assert trigger.style_filter.allow_default_foreground is True
    assert trigger.style_filter.allow_default_background is True


def test_automation_master_switch_and_priority_round_trip(tmp_path):
    path = tmp_path / "automation.json"
    engine = AutomationEngine(send_fn=lambda _line: None)
    engine.set_master_enabled(False)
    engine.add_simple_trigger(r"^hi$", "wave", priority=25)
    save_automation(engine, str(path))

    restored = AutomationEngine(send_fn=lambda _line: None)
    load_automation(restored, str(path))
    trigger = next(iter(restored.triggers.values()))
    assert restored.enabled is False
    assert trigger.priority == 25


def test_profile_scoped_paths_are_stable_and_separate():
    from persistence import profile_automation_path, profile_macros_path

    first = profile_automation_path("Realms of Despair")
    assert first == profile_automation_path("Realms of Despair")
    assert first != profile_automation_path("Discworld")
    assert profile_macros_path("Realms of Despair") != first


def test_load_profiles_safely_preserves_bad_file_and_returns_empty(tmp_path):
    from persistence import load_profiles_safely

    path = tmp_path / "profiles.json"
    original = '{"broken": '
    path.write_text(original, encoding="utf-8")

    profiles, error = load_profiles_safely(str(path))

    assert profiles == {}
    assert error is not None
    assert "invalid JSON" in error
    assert path.read_text(encoding="utf-8") == original


def test_atomic_json_replace_failure_preserves_previous_file_and_cleans_temp(tmp_path, monkeypatch):
    import persistence

    path = tmp_path / "profiles.json"
    original = b'{"existing": {"host": "old.example", "port": 23}}\n'
    path.write_bytes(original)

    def fail_replace(_source, _target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(persistence.os, "replace", fail_replace)

    try:
        persistence._atomic_write_json(path, {"new": {"host": "new.example", "port": 4242}})
    except OSError as exc:
        assert "simulated replace failure" in str(exc)
    else:
        raise AssertionError("replace failure unexpectedly succeeded")

    assert path.read_bytes() == original
    assert list(tmp_path.glob(".profiles.json.*.tmp")) == []


def test_atomic_json_fsyncs_file_before_replace(tmp_path, monkeypatch):
    import persistence

    path = tmp_path / "state.json"
    events = []
    real_fsync = persistence.os.fsync
    real_replace = persistence.os.replace

    def recording_fsync(fd):
        events.append("fsync")
        return real_fsync(fd)

    def recording_replace(source, target):
        events.append("replace")
        return real_replace(source, target)

    monkeypatch.setattr(persistence.os, "fsync", recording_fsync)
    monkeypatch.setattr(persistence.os, "replace", recording_replace)

    persistence._atomic_write_json(path, {"safe": True})

    assert "replace" in events
    replace_index = events.index("replace")
    assert "fsync" in events[:replace_index]
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_deeply_nested_json_is_normalized_to_fail_soft_validation_error(tmp_path):
    from persistence import load_profiles_safely

    path = tmp_path / "profiles.json"
    path.write_text("[" * 10000 + "0" + "]" * 10000, encoding="utf-8")

    profiles, error = load_profiles_safely(str(path))

    assert profiles == {}
    assert error is not None
    assert "nesting is too deep" in error


def test_variable_round_trip_preserves_scalar_types(tmp_path):
    from persistence import load_variables, save_variables
    from variables import VariableStore

    path = tmp_path / "variables.json"
    source = VariableStore({"name": "Ada", "count": 4, "ratio": 1.25, "ready": True})
    save_variables(source, str(path))

    restored = load_variables(str(path))
    assert restored.as_dict() == {
        "name": "Ada",
        "count": 4,
        "ratio": 1.25,
        "ready": True,
    }


def test_variable_profile_paths_are_stable_and_distinct():
    from persistence import profile_automation_path, profile_variables_path

    first = profile_variables_path("Realms of Despair")
    assert first == profile_variables_path("Realms of Despair")
    assert first != profile_variables_path("Discworld")
    assert first != profile_automation_path("Realms of Despair")
