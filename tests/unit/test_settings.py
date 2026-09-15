from __future__ import annotations

import json

import pytest

from client_settings import ClientSettings
from persistence import load_settings, load_settings_safely, save_settings


def test_settings_defaults_validate_and_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    settings = ClientSettings(
        font_family="JetBrains Mono",
        font_size=13,
        output_foreground="#abcdef",
        output_background="#101112",
        scrollback_blocks=25_000,
        timestamps=True,
        local_echo=False,
        default_auto_reconnect=False,
        default_reconnect_base_delay=2.5,
        default_reconnect_max_delay=30.0,
    )

    save_settings(settings, path)
    assert load_settings(path) == settings

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["_mudclient"] == {
        "format": "python-mud-client",
        "kind": "settings",
        "version": 1,
    }
    assert raw["data"]["font_size"] == 13


def test_settings_safe_loader_leaves_malformed_file_untouched(tmp_path):
    path = tmp_path / "settings.json"
    original = b'{"_mudclient": '
    path.write_bytes(original)

    settings, error = load_settings_safely(path)

    assert settings == ClientSettings()
    assert error is not None
    assert path.read_bytes() == original


def test_settings_future_version_is_fail_soft_and_not_rewritten(tmp_path):
    path = tmp_path / "settings.json"
    document = {
        "_mudclient": {
            "format": "python-mud-client",
            "kind": "settings",
            "version": 999,
        },
        "data": ClientSettings().to_dict(),
    }
    original = (json.dumps(document) + "\n").encode()
    path.write_bytes(original)

    settings, error = load_settings_safely(path)

    assert settings == ClientSettings()
    assert "newer than this client supports" in error
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "field,value",
    [
        ("font_size", 2),
        ("output_foreground", "red"),
        ("scrollback_blocks", 0),
        ("local_echo", "yes"),
        ("default_reconnect_base_delay", 0),
    ],
)
def test_settings_reject_invalid_values(field, value):
    data = ClientSettings().to_dict()
    data[field] = value
    with pytest.raises(ValueError):
        ClientSettings.from_dict(data)


def test_settings_reject_unknown_fields():
    data = ClientSettings().to_dict()
    data["mystery"] = True
    with pytest.raises(ValueError, match="unknown settings"):
        ClientSettings.from_dict(data)


def test_settings_save_refuses_to_overwrite_future_version(tmp_path):
    path = tmp_path / "settings.json"
    document = {
        "_mudclient": {
            "format": "python-mud-client",
            "kind": "settings",
            "version": 999,
        },
        "data": ClientSettings().to_dict(),
    }
    original = (json.dumps(document, sort_keys=True) + "\n").encode()
    path.write_bytes(original)

    with pytest.raises(ValueError, match="newer than this client supports"):
        save_settings(ClientSettings(font_size=14), path)

    assert path.read_bytes() == original


def test_legacy_settings_first_save_creates_byte_exact_backup(tmp_path):
    path = tmp_path / "settings.json"
    legacy = b'{"font_size": 11, "timestamps": true}\n'
    path.write_bytes(legacy)

    loaded = load_settings(path)
    assert loaded.font_size == 11
    assert loaded.timestamps is True

    save_settings(loaded, path)

    backup = tmp_path / "settings.json.pre-v1.bak"
    assert backup.read_bytes() == legacy
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["_mudclient"]["version"] == 1
    assert raw["_mudclient"]["kind"] == "settings"
