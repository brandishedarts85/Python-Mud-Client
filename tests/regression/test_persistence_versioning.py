import json

import pytest

from automation import AutomationEngine
from persistence import (
    PERSISTENCE_FORMAT,
    PERSISTENCE_KIND_AUTOMATION,
    PERSISTENCE_KIND_MACROS,
    PERSISTENCE_KIND_PROFILES,
    PERSISTENCE_SCHEMA_VERSION,
    PersistenceVersionError,
    load_automation,
    load_macros,
    load_profiles,
    load_profiles_safely,
    migrate_legacy_persistence,
    persistence_document_version,
    save_automation,
    save_macros,
    save_profile,
)


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_envelope(path, kind):
    doc = _read(path)
    assert doc["_mudclient"] == {
        "format": PERSISTENCE_FORMAT,
        "kind": kind,
        "version": PERSISTENCE_SCHEMA_VERSION,
    }
    assert "data" in doc
    return doc["data"]


def test_new_saves_use_same_v1_envelope_for_all_persistence_kinds(tmp_path):
    profiles = tmp_path / "profiles.json"
    automation = tmp_path / "automation.json"
    macros = tmp_path / "macros.json"

    save_profile("test", "example.test", 4000, path=str(profiles))

    engine = AutomationEngine(send_fn=lambda _line: None)
    engine.add_alias("l", "look")
    save_automation(engine, str(automation))

    save_macros(
        {
            "enabled": True,
            "bindings": {"base": {}, "ctrl": {}, "alt": {}},
        },
        str(macros),
    )

    assert "test" in _assert_envelope(profiles, PERSISTENCE_KIND_PROFILES)
    assert _assert_envelope(automation, PERSISTENCE_KIND_AUTOMATION)["aliases"]
    assert _assert_envelope(macros, PERSISTENCE_KIND_MACROS)["enabled"] is True


def test_legacy_reads_are_side_effect_free(tmp_path):
    path = tmp_path / "profiles.json"
    original = b'{"old": {"host": "legacy.test", "port": 23}}\n'
    path.write_bytes(original)

    assert load_profiles(str(path))["old"]["host"] == "legacy.test"
    assert persistence_document_version(path, kind=PERSISTENCE_KIND_PROFILES) == 0
    assert path.read_bytes() == original
    assert not (tmp_path / "profiles.json.pre-v1.bak").exists()


def test_first_profile_save_migrates_and_preserves_exact_legacy_bytes(tmp_path):
    path = tmp_path / "profiles.json"
    original = b'{"old": {"host": "legacy.test", "port": 23}}\r\n'
    path.write_bytes(original)

    save_profile("new", "new.test", 4242, path=str(path))

    backup = tmp_path / "profiles.json.pre-v1.bak"
    assert backup.read_bytes() == original
    assert persistence_document_version(path, kind=PERSISTENCE_KIND_PROFILES) == 1
    loaded = load_profiles(str(path))
    assert set(loaded) == {"old", "new"}


def test_first_automation_save_migrates_and_backup_is_one_shot(tmp_path):
    path = tmp_path / "automation.json"
    original = b'{"enabled": true, "aliases": [{"pattern": "l", "expansion": "look"}], "triggers": []}\n'
    path.write_bytes(original)

    engine = AutomationEngine(send_fn=lambda _line: None)
    load_automation(engine, str(path))
    engine.add_alias("i", "inventory")
    save_automation(engine, str(path))

    backup = tmp_path / "automation.json.pre-v1.bak"
    assert backup.read_bytes() == original

    first_backup = backup.read_bytes()
    engine.add_alias("s", "score")
    save_automation(engine, str(path))
    assert backup.read_bytes() == first_backup

    restored = AutomationEngine(send_fn=lambda _line: None)
    aliases, _triggers = load_automation(restored, str(path))
    assert aliases == 3


def test_first_macro_save_migrates_and_preserves_legacy_bytes(tmp_path):
    path = tmp_path / "macros.json"
    original = b'{"enabled": false, "bindings": {"base": {"8": {"label": "N", "command": "north", "enabled": true}}}}\n'
    path.write_bytes(original)

    config = load_macros(str(path))
    save_macros(config, str(path))

    assert (tmp_path / "macros.json.pre-v1.bak").read_bytes() == original
    assert persistence_document_version(path, kind=PERSISTENCE_KIND_MACROS) == 1
    assert load_macros(str(path))["bindings"]["base"]["8"]["command"] == "north"


def test_explicit_migration_validates_then_backs_up_and_upgrades(tmp_path):
    path = tmp_path / "profiles.json"
    original = b'{"old": {"host": "legacy.test", "port": 23}}'
    path.write_bytes(original)

    backup = migrate_legacy_persistence(path, kind=PERSISTENCE_KIND_PROFILES)

    assert backup == str(tmp_path / "profiles.json.pre-v1.bak")
    assert (tmp_path / "profiles.json.pre-v1.bak").read_bytes() == original
    assert persistence_document_version(path, kind=PERSISTENCE_KIND_PROFILES) == 1
    assert migrate_legacy_persistence(path, kind=PERSISTENCE_KIND_PROFILES) is None


def test_explicit_migration_does_not_backup_invalid_legacy_data(tmp_path):
    path = tmp_path / "profiles.json"
    original = b'{"bad": {"host": "", "port": 23}}'
    path.write_bytes(original)

    with pytest.raises(ValueError):
        migrate_legacy_persistence(path, kind=PERSISTENCE_KIND_PROFILES)

    assert path.read_bytes() == original
    assert not (tmp_path / "profiles.json.pre-v1.bak").exists()


def test_future_schema_is_rejected_fail_soft_and_never_overwritten(tmp_path):
    path = tmp_path / "profiles.json"
    future = {
        "_mudclient": {
            "format": PERSISTENCE_FORMAT,
            "kind": PERSISTENCE_KIND_PROFILES,
            "version": PERSISTENCE_SCHEMA_VERSION + 1,
        },
        "data": {"x": {"host": "future.test", "port": 4000}},
    }
    original = json.dumps(future).encode("utf-8")
    path.write_bytes(original)

    profiles, error = load_profiles_safely(str(path))
    assert profiles == {}
    assert error is not None and "newer than this client supports" in error

    with pytest.raises(PersistenceVersionError):
        save_profile("new", "new.test", 4242, path=str(path))

    assert path.read_bytes() == original
    assert not (tmp_path / "profiles.json.pre-v1.bak").exists()


def test_versioned_document_kind_mismatch_is_rejected(tmp_path):
    path = tmp_path / "automation.json"
    path.write_text(
        json.dumps(
            {
                "_mudclient": {
                    "format": PERSISTENCE_FORMAT,
                    "kind": PERSISTENCE_KIND_MACROS,
                    "version": 1,
                },
                "data": {"enabled": True, "bindings": {}},
            }
        ),
        encoding="utf-8",
    )

    engine = AutomationEngine(send_fn=lambda _line: None)
    with pytest.raises(ValueError, match="document kind"):
        load_automation(engine, str(path))


def test_legacy_profile_named_reserved_marker_is_not_misclassified(tmp_path):
    path = tmp_path / "profiles.json"
    path.write_text(
        json.dumps(
            {
                "_mudclient": {"host": "marker-name.test", "port": 4000},
                "normal": {"host": "normal.test", "port": 4001},
            }
        ),
        encoding="utf-8",
    )

    profiles = load_profiles(str(path))
    assert profiles["_mudclient"]["host"] == "marker-name.test"
    assert persistence_document_version(path, kind=PERSISTENCE_KIND_PROFILES) == 0


def test_macro_writer_rejects_values_reader_would_reject(tmp_path):
    path = tmp_path / "macros.json"
    bad = {
        "enabled": "yes",
        "bindings": {"base": {}, "ctrl": {}, "alt": {}},
    }
    with pytest.raises(ValueError, match="expected boolean"):
        save_macros(bad, str(path))
    assert not path.exists()


def test_failed_migration_write_keeps_legacy_source_and_backup(tmp_path, monkeypatch):
    import persistence

    path = tmp_path / "profiles.json"
    original = b'{"old": {"host": "legacy.test", "port": 23}}\n'
    path.write_bytes(original)

    real_replace = persistence.os.replace
    calls = {"count": 0}

    def fail_second_replace(source, target):
        calls["count"] += 1
        # First replace commits the pre-migration backup; second would replace
        # the live legacy file with the v1 document.
        if calls["count"] == 2:
            raise OSError("simulated migration replace failure")
        return real_replace(source, target)

    monkeypatch.setattr(persistence.os, "replace", fail_second_replace)

    with pytest.raises(OSError, match="simulated migration replace failure"):
        save_profile("new", "new.test", 4242, path=str(path))

    assert path.read_bytes() == original
    assert (tmp_path / "profiles.json.pre-v1.bak").read_bytes() == original
    assert list(tmp_path.glob(".profiles.json.*.tmp")) == []


def test_variables_join_common_v1_envelope_and_legacy_migration(tmp_path):
    from persistence import (
        PERSISTENCE_KIND_VARIABLES,
        load_variables,
        save_variables,
    )
    from variables import VariableStore

    path = tmp_path / "variables.json"
    original = b'{"target": "orc", "count": 2}\n'
    path.write_bytes(original)

    loaded = load_variables(str(path))
    assert loaded.get("target") == "orc"
    assert path.read_bytes() == original

    loaded.set("ready", True)
    save_variables(loaded, str(path))

    assert (tmp_path / "variables.json.pre-v1.bak").read_bytes() == original
    assert persistence_document_version(path, kind=PERSISTENCE_KIND_VARIABLES) == 1
    assert _assert_envelope(path, PERSISTENCE_KIND_VARIABLES)["ready"] is True


def test_future_variable_schema_is_rejected_without_overwrite(tmp_path):
    from persistence import (
        PERSISTENCE_KIND_VARIABLES,
        load_variables_safely,
        save_variables,
    )
    from variables import VariableStore

    path = tmp_path / "variables.json"
    future = {
        "_mudclient": {
            "format": PERSISTENCE_FORMAT,
            "kind": PERSISTENCE_KIND_VARIABLES,
            "version": PERSISTENCE_SCHEMA_VERSION + 1,
        },
        "data": {"target": "future"},
    }
    original = json.dumps(future).encode("utf-8")
    path.write_bytes(original)

    store, error = load_variables_safely(str(path))
    assert store.as_dict() == {}
    assert error is not None and "newer than this client supports" in error

    with pytest.raises(PersistenceVersionError):
        save_variables(VariableStore({"new": "value"}), str(path))
    assert path.read_bytes() == original
