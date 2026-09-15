"""
persistence.py -- small JSON-backed save/load for connection profiles
and user-authored automation.

This is intentionally not a database.

JSON-backed user state includes:

    profiles.json
    automation.json
    macros.json
    settings.json
    variables.json

Only persistence-safe automation is serialized:

    aliases whose expansion is a string
    simple triggers with a response_template

Code-defined callables remain code-owned runtime behavior and are never
serialized by this module.

Persistence rules:
  - validate loaded structure before mutating live engine state
  - reject malformed entries clearly
  - write atomically through a temporary file + replace
  - preserve UTF-8 text
  - never partially apply a malformed automation file
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile

from pathlib import Path
from typing import Any

from automation import AutomationEngine, TriggerStyleFilter
from client_settings import ClientSettings
from variables import MAX_VARIABLES, VariableStore, validate_variable_name, validate_variable_value


DEFAULT_PROFILES_PATH = "profiles.json"
DEFAULT_AUTOMATION_PATH = "automation.json"
DEFAULT_MACROS_PATH = "macros.json"
DEFAULT_SETTINGS_PATH = "settings.json"
DEFAULT_VARIABLES_PATH = "variables.json"
DEFAULT_PROFILE_DATA_DIR = "profiles_data"

MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_JSON_NESTING = 1024
MAX_PROFILES = 1000
MAX_ALIASES = 10_000
MAX_TRIGGERS = 10_000

PERSISTENCE_FORMAT = "python-mud-client"
PERSISTENCE_SCHEMA_VERSION = 1
PERSISTENCE_KIND_PROFILES = "profiles"
PERSISTENCE_KIND_AUTOMATION = "automation"
PERSISTENCE_KIND_MACROS = "macros"
PERSISTENCE_KIND_SETTINGS = "settings"
PERSISTENCE_KIND_VARIABLES = "variables"


class PersistenceVersionError(ValueError):
    """Raised when persisted data uses a schema version this client cannot read."""



# ---------------------------------------------------------------------------
# Profile-scoped user data paths
# ---------------------------------------------------------------------------


def _profile_scope_stem(profile_name: str) -> str:
    name = str(profile_name).strip()
    if not name:
        raise ValueError("profile name must not be empty")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-._") or "profile"
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:10]
    return f"{slug[:64]}-{digest}"


def profile_automation_path(
    profile_name: str,
    *,
    root: str | Path = DEFAULT_PROFILE_DATA_DIR,
) -> str:
    return str(Path(root) / f"{_profile_scope_stem(profile_name)}.automation.json")


def profile_macros_path(
    profile_name: str,
    *,
    root: str | Path = DEFAULT_PROFILE_DATA_DIR,
) -> str:
    return str(Path(root) / f"{_profile_scope_stem(profile_name)}.macros.json")


def profile_variables_path(
    profile_name: str,
    *,
    root: str | Path = DEFAULT_PROFILE_DATA_DIR,
) -> str:
    return str(Path(root) / f"{_profile_scope_stem(profile_name)}.variables.json")


def profile_mapper_path(
    profile_name: str,
    *,
    root: str | Path = DEFAULT_PROFILE_DATA_DIR,
) -> str:
    """Return the deterministic SQLite map path for one saved profile."""
    return str(Path(root) / f"{_profile_scope_stem(profile_name)}.mapper.sqlite3")


# ---------------------------------------------------------------------------
# Generic JSON helpers
# ---------------------------------------------------------------------------


def _json_depth(text: str) -> int:
    """Estimate nesting depth without materializing giant Python objects."""
    depth = 0
    max_depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue

        if char in "[{":
            depth += 1
            if depth > max_depth:
                max_depth = depth
        elif char in "]}":
            depth = max(0, depth - 1)

    return max_depth


def _read_json_file(
    path: str | Path,
) -> Any:
    p = Path(path)

    if not p.exists():
        return None

    try:
        size = p.stat().st_size
    except OSError as exc:
        raise ValueError(
            f"{p}: unable to inspect file: {exc}"
        ) from exc

    if size > MAX_JSON_BYTES:
        raise ValueError(
            f"{p}: JSON file exceeds maximum size "
            f"of {MAX_JSON_BYTES} bytes"
        )

    try:
        with p.open(
            "r",
            encoding="utf-8",
        ) as file:
            text = file.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(
            f"{p}: unable to read file: {exc}"
        ) from exc

    if _json_depth(text) > MAX_JSON_NESTING:
        raise ValueError(
            f"{p}: JSON nesting is too deep"
        )

    try:
        return json.loads(text)

    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{p}: invalid JSON at line "
            f"{exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc

    except RecursionError as exc:
        # Deeply nested persisted JSON can overflow Python's JSON decoder
        # recursion before any schema validator gets a chance to inspect it.
        raise ValueError(
            f"{p}: JSON nesting is too deep"
        ) from exc


def _fsync_parent_directory(path: Path) -> None:
    """Best-effort durability barrier for the directory entry on POSIX.

    fsync() of the temporary file protects the file contents.  After
    os.replace(), syncing the containing directory makes the rename itself
    durable across a sudden power loss on filesystems that support directory
    fsync.  Windows does not expose directory fsync through this interface.
    """

    if os.name == "nt":
        return

    directory = path.parent if str(path.parent) not in ("", ".") else Path(".")
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY

    try:
        dir_fd = os.open(directory, flags)
    except OSError:
        # Some platforms/filesystems do not permit opening directories this
        # way.  The file itself was still fsynced before replacement.
        return

    try:
        os.fsync(dir_fd)
    except OSError:
        # Directory fsync support varies by platform/filesystem.
        pass
    finally:
        os.close(dir_fd)


def _atomic_write_json(
    path: str | Path,
    data: Any,
) -> None:
    """
    Write JSON atomically.

    The new file is fully written and flushed before replacing the previous
    file, reducing the chance of a crash leaving a half-written JSON document.
    """

    target = Path(path)

    parent = target.parent

    if str(parent) not in ("", "."):
        parent.mkdir(
            parents=True,
            exist_ok=True,
        )

    temp_dir = (
        parent
        if str(parent) not in ("", ".")
        else Path(".")
    )

    fd: int | None = None
    temp_name: str | None = None

    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=temp_dir,
            text=True,
        )

        with os.fdopen(
            fd,
            "w",
            encoding="utf-8",
            newline="\n",
        ) as file:
            fd = None

            json.dump(
                data,
                file,
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )

            file.write("\n")
            file.flush()
            os.fsync(
                file.fileno()
            )

        os.replace(
            temp_name,
            target,
        )

        temp_name = None
        _fsync_parent_directory(target)

    finally:
        if fd is not None:
            os.close(fd)

        if temp_name is not None:
            try:
                os.unlink(
                    temp_name
                )
            except FileNotFoundError:
                pass



def _document_envelope(kind: str, payload: Any) -> dict[str, Any]:
    return {
        "_mudclient": {
            "format": PERSISTENCE_FORMAT,
            "kind": kind,
            "version": PERSISTENCE_SCHEMA_VERSION,
        },
        "data": payload,
    }


def _unwrap_document(
    data: Any,
    *,
    path: str,
    kind: str,
) -> tuple[int, Any]:
    """Return ``(version, payload)`` for legacy or versioned documents.

    Legacy files are version 0 and retain their historical top-level shape.
    A versioned envelope is recognized only when the reserved ``_mudclient``
    marker explicitly identifies this application's persistence format.  This
    avoids misclassifying a legacy connection profile that happens to be named
    ``_mudclient``.
    """

    if not isinstance(data, dict):
        return 0, data

    marker = data.get("_mudclient")
    if not (
        isinstance(marker, dict)
        and marker.get("format") == PERSISTENCE_FORMAT
    ):
        return 0, data

    unknown_top = set(data) - {"_mudclient", "data"}
    if unknown_top:
        raise ValueError(
            f"{path}: unknown versioned document field(s): "
            f"{', '.join(sorted(unknown_top))}"
        )

    unknown_marker = set(marker) - {"format", "kind", "version"}
    if unknown_marker:
        raise ValueError(
            f"{path}: unknown persistence metadata field(s): "
            f"{', '.join(sorted(unknown_marker))}"
        )

    if marker.get("kind") != kind:
        raise ValueError(
            f"{path}: persistence document kind is {marker.get('kind')!r}, "
            f"expected {kind!r}"
        )

    version = marker.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError(f"{path}: persistence schema version must be an integer")
    if version < 1:
        raise ValueError(f"{path}: invalid persistence schema version {version}")
    if version > PERSISTENCE_SCHEMA_VERSION:
        raise PersistenceVersionError(
            f"{path}: persistence schema version {version} is newer than "
            f"this client supports ({PERSISTENCE_SCHEMA_VERSION})"
        )
    if "data" not in data:
        raise ValueError(f"{path}: versioned persistence document is missing 'data'")

    # Future migration chains can be inserted here.  Version 1 is currently
    # the only versioned format, while unversioned historical files are v0.
    return version, data["data"]


def _atomic_write_bytes(path: str | Path, payload: bytes) -> None:
    """Atomically write raw bytes with the same durability rules as JSON."""

    target = Path(path)
    parent = target.parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)
    temp_dir = parent if str(parent) not in ("", ".") else Path(".")

    fd: int | None = None
    temp_name: str | None = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=temp_dir
        )
        with os.fdopen(fd, "wb") as file:
            fd = None
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, target)
        temp_name = None
        _fsync_parent_directory(target)
    finally:
        if fd is not None:
            os.close(fd)
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def _ensure_pre_migration_backup(path: str | Path, *, kind: str) -> str | None:
    """Preserve the original bytes before the first legacy-to-v1 rewrite.

    The backup is deliberately one-shot and byte-for-byte.  If migration or
    the subsequent save fails, the backup remains available.  Current-version
    files are not backed up on every ordinary save.
    """

    target = Path(path)
    if not target.exists():
        return None

    raw = _read_json_file(target)
    version, _payload = _unwrap_document(raw, path=str(target), kind=kind)
    if version >= PERSISTENCE_SCHEMA_VERSION:
        return None

    backup = target.with_name(
        f"{target.name}.pre-v{PERSISTENCE_SCHEMA_VERSION}.bak"
    )
    if not backup.exists():
        _atomic_write_bytes(backup, target.read_bytes())
    return str(backup)


def _atomic_write_document(path: str | Path, *, kind: str, payload: Any) -> None:
    """Write the current persistence envelope, backing up legacy input first."""

    _ensure_pre_migration_backup(path, kind=kind)
    _atomic_write_json(path, _document_envelope(kind, payload))


def _require_string(
    value: Any,
    *,
    field: str,
) -> str:
    if not isinstance(
        value,
        str,
    ):
        raise ValueError(
            f"{field}: expected string"
        )

    return value


def _require_bool(
    value: Any,
    *,
    field: str,
) -> bool:
    if not isinstance(
        value,
        bool,
    ):
        raise ValueError(
            f"{field}: expected boolean"
        )

    return value


def _require_port(
    value: Any,
    *,
    field: str = "port",
) -> int:
    if isinstance(
        value,
        bool,
    ):
        raise ValueError(
            f"{field}: expected integer port"
        )

    try:
        port = int(
            value
        )

    except (
        TypeError,
        ValueError,
    ) as exc:
        raise ValueError(
            f"{field}: expected integer port"
        ) from exc

    if not (
        1 <= port <= 65535
    ):
        raise ValueError(
            f"{field}: must be between 1 and 65535"
        )

    return port


def _require_nonnegative_finite_float(
    value: Any,
    *,
    field: str,
) -> float:
    if isinstance(
        value,
        bool,
    ):
        raise ValueError(
            f"{field}: expected number"
        )

    try:
        number = float(
            value
        )

    except (
        TypeError,
        ValueError,
    ) as exc:
        raise ValueError(
            f"{field}: expected number"
        ) from exc

    if not math.isfinite(
        number
    ):
        raise ValueError(
            f"{field}: must be finite"
        )

    if number < 0:
        raise ValueError(
            f"{field}: must be >= 0"
        )

    return number


# ---------------------------------------------------------------------------
# Connection profiles
# ---------------------------------------------------------------------------


def load_profiles(
    path: str = DEFAULT_PROFILES_PATH,
) -> dict[str, dict[str, Any]]:
    """
    Return:

        {
            "name": {
                "host": "...",
                "port": 4000
            }
        }

    Missing file returns {}.

    The full file is validated before anything is returned.
    """

    data = _read_json_file(
        path
    )

    if data is None:
        return {}

    _version, data = _unwrap_document(
        data, path=path, kind=PERSISTENCE_KIND_PROFILES
    )

    if not isinstance(
        data,
        dict,
    ):
        raise ValueError(
            f"{path}: expected a JSON object of profiles"
        )

    if len(data) > MAX_PROFILES:
        raise ValueError(
            f"{path}: too many profiles "
            f"(maximum {MAX_PROFILES})"
        )

    profiles: dict[
        str,
        dict[str, Any],
    ] = {}

    for name, raw_profile in data.items():
        if not isinstance(
            name,
            str,
        ):
            raise ValueError(
                f"{path}: profile names must be strings"
            )

        if not name.strip():
            raise ValueError(
                f"{path}: profile name must not be empty"
            )

        if not isinstance(
            raw_profile,
            dict,
        ):
            raise ValueError(
                f"{path}: profile {name!r} must be an object"
            )

        unknown = set(
            raw_profile
        ) - {
            "host",
            "port",
            "terminal_type",
            "auto_reconnect",
            "reconnect_base_delay",
            "reconnect_max_delay",
        }

        if unknown:
            raise ValueError(
                f"{path}: profile {name!r} contains "
                f"unknown field(s): {', '.join(sorted(unknown))}"
            )

        if "host" not in raw_profile:
            raise ValueError(
                f"{path}: profile {name!r} is missing 'host'"
            )

        if "port" not in raw_profile:
            raise ValueError(
                f"{path}: profile {name!r} is missing 'port'"
            )

        host = _require_string(
            raw_profile["host"],
            field=f"profile {name!r}.host",
        ).strip()

        if not host:
            raise ValueError(
                f"{path}: profile {name!r} host must not be empty"
            )

        port = _require_port(
            raw_profile["port"],
            field=f"profile {name!r}.port",
        )

        terminal_type = _require_string(
            raw_profile.get("terminal_type", "xterm-256color"),
            field=f"profile {name!r}.terminal_type",
        ).strip()
        if not terminal_type:
            raise ValueError(
                f"{path}: profile {name!r} terminal_type must not be empty"
            )

        auto_reconnect = _require_bool(
            raw_profile.get("auto_reconnect", True),
            field=f"profile {name!r}.auto_reconnect",
        )
        reconnect_base_delay = _require_nonnegative_finite_float(
            raw_profile.get("reconnect_base_delay", 3.0),
            field=f"profile {name!r}.reconnect_base_delay",
        )
        reconnect_max_delay = _require_nonnegative_finite_float(
            raw_profile.get("reconnect_max_delay", 60.0),
            field=f"profile {name!r}.reconnect_max_delay",
        )
        if reconnect_base_delay <= 0:
            raise ValueError(
                f"{path}: profile {name!r} reconnect_base_delay must be > 0"
            )
        if reconnect_max_delay < reconnect_base_delay:
            raise ValueError(
                f"{path}: profile {name!r} reconnect_max_delay must be >= reconnect_base_delay"
            )

        profiles[
            name
        ] = {
            "host": host,
            "port": port,
            "terminal_type": terminal_type,
            "auto_reconnect": auto_reconnect,
            "reconnect_base_delay": reconnect_base_delay,
            "reconnect_max_delay": reconnect_max_delay,
        }

    return profiles


def load_profiles_safely(
    path: str = DEFAULT_PROFILES_PATH,
) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Fail-soft application wrapper around strict profile validation.

    ``load_profiles`` remains the authoritative strict validator.  This helper
    is for startup/UI surfaces that must stay usable when persisted user data
    is malformed.  The source file is left untouched so the user can repair or
    recover it.
    """
    try:
        return load_profiles(path), None
    except (ValueError, OSError) as exc:
        return {}, str(exc)


def save_profile(
    name: str,
    host: str,
    port: int,
    path: str = DEFAULT_PROFILES_PATH,
    *,
    terminal_type: str = "xterm-256color",
    auto_reconnect: bool = True,
    reconnect_base_delay: float = 3.0,
    reconnect_max_delay: float = 60.0,
) -> None:
    name = _require_string(
        name,
        field="profile name",
    ).strip()

    host = _require_string(
        host,
        field="host",
    ).strip()

    if not name:
        raise ValueError(
            "profile name must not be empty"
        )

    if not host:
        raise ValueError(
            "host must not be empty"
        )

    port = _require_port(
        port
    )
    terminal_type = _require_string(
        terminal_type, field="terminal_type"
    ).strip()
    if not terminal_type:
        raise ValueError("terminal_type must not be empty")
    auto_reconnect = _require_bool(
        auto_reconnect, field="auto_reconnect"
    )
    reconnect_base_delay = _require_nonnegative_finite_float(
        reconnect_base_delay, field="reconnect_base_delay"
    )
    reconnect_max_delay = _require_nonnegative_finite_float(
        reconnect_max_delay, field="reconnect_max_delay"
    )
    if reconnect_base_delay <= 0:
        raise ValueError("reconnect_base_delay must be > 0")
    if reconnect_max_delay < reconnect_base_delay:
        raise ValueError("reconnect_max_delay must be >= reconnect_base_delay")

    profiles = load_profiles(
        path
    )

    profiles[
        name
    ] = {
        "host": host,
        "port": port,
        "terminal_type": terminal_type,
        "auto_reconnect": auto_reconnect,
        "reconnect_base_delay": reconnect_base_delay,
        "reconnect_max_delay": reconnect_max_delay,
    }

    if len(profiles) > MAX_PROFILES:
        raise ValueError(
            f"too many profiles "
            f"(maximum {MAX_PROFILES})"
        )

    _atomic_write_document(
        path,
        kind=PERSISTENCE_KIND_PROFILES,
        payload=profiles,
    )



def delete_profile(
    name: str,
    path: str = DEFAULT_PROFILES_PATH,
) -> bool:
    """Delete a saved connection profile. Returns True if it existed."""
    name = _require_string(name, field="profile name").strip()
    if not name:
        raise ValueError("profile name must not be empty")
    profiles = load_profiles(path)
    if name not in profiles:
        return False
    del profiles[name]
    _atomic_write_document(
        path, kind=PERSISTENCE_KIND_PROFILES, payload=profiles
    )
    return True

def resolve_connection(
    arg1: str | None,
    arg2: str | None,
    path: str = DEFAULT_PROFILES_PATH,
) -> tuple[str | None, int | None]:
    """
    CLI-friendly connection resolution.

    Rules:

        no args
            -> (None, None)

        one saved profile name
            -> (profile host, profile port)

        one unknown string
            -> (that string as host, None)

        host + port
            -> (host, parsed port)

    Explicit host + port bypasses profile lookup.
    """

    if arg1 and arg2:
        host = arg1.strip()

        if not host:
            raise ValueError(
                "host must not be empty"
            )

        return (
            host,
            _require_port(
                arg2
            ),
        )

    if arg1:
        profiles = load_profiles(
            path
        )

        if arg1 in profiles:
            profile = profiles[
                arg1
            ]

            return (
                profile["host"],
                profile["port"],
            )

        host = arg1.strip()

        if not host:
            return None, None

        return (
            host,
            None,
        )

    return (
        None,
        None,
    )


# ---------------------------------------------------------------------------
# Automation serialization
# ---------------------------------------------------------------------------


def _style_filter_to_dict(style_filter: TriggerStyleFilter | None) -> dict[str, Any] | None:
    if style_filter is None or not style_filter.active:
        return None
    return {
        "foregrounds": [list(rgb) for rgb in style_filter.foregrounds],
        "backgrounds": [list(rgb) for rgb in style_filter.backgrounds],
        "allow_default_foreground": style_filter.allow_default_foreground,
        "allow_default_background": style_filter.allow_default_background,
        "bold": style_filter.bold,
        "dim": style_filter.dim,
        "italic": style_filter.italic,
        "underline": style_filter.underline,
        "blink": style_filter.blink,
        "strike": style_filter.strike,
    }


def _validate_rgb_list(value: Any, *, field: str) -> tuple[tuple[int, int, int], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field}: expected array of RGB triples")
    colors: list[tuple[int, int, int]] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, (list, tuple)) or len(raw) != 3:
            raise ValueError(f"{field}[{index}]: expected RGB triple")
        rgb: list[int] = []
        for channel, component in enumerate(raw):
            if isinstance(component, bool) or not isinstance(component, int) or not 0 <= component <= 255:
                raise ValueError(f"{field}[{index}][{channel}]: expected integer 0..255")
            rgb.append(component)
        item = (rgb[0], rgb[1], rgb[2])
        if item not in colors:
            colors.append(item)
    return tuple(colors)


def _validate_optional_bool(value: Any, *, field: str) -> bool | None:
    if value is None:
        return None
    return _require_bool(value, field=field)


def _validate_style_filter(value: Any, *, field: str) -> TriggerStyleFilter | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{field}: expected object or null")
    allowed = {
        "foregrounds", "backgrounds",
        "allow_default_foreground", "allow_default_background",
        "bold", "dim", "italic", "underline", "blink", "strike",
    }
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{field}: unknown field(s): {', '.join(sorted(unknown))}")
    filt = TriggerStyleFilter(
        foregrounds=_validate_rgb_list(value.get("foregrounds", []), field=f"{field}.foregrounds"),
        backgrounds=_validate_rgb_list(value.get("backgrounds", []), field=f"{field}.backgrounds"),
        allow_default_foreground=_require_bool(
            value.get("allow_default_foreground", False),
            field=f"{field}.allow_default_foreground",
        ),
        allow_default_background=_require_bool(
            value.get("allow_default_background", False),
            field=f"{field}.allow_default_background",
        ),
        bold=_validate_optional_bool(value.get("bold"), field=f"{field}.bold"),
        dim=_validate_optional_bool(value.get("dim"), field=f"{field}.dim"),
        italic=_validate_optional_bool(value.get("italic"), field=f"{field}.italic"),
        underline=_validate_optional_bool(value.get("underline"), field=f"{field}.underline"),
        blink=_validate_optional_bool(value.get("blink"), field=f"{field}.blink"),
        strike=_validate_optional_bool(value.get("strike"), field=f"{field}.strike"),
    )
    return filt if filt.active else None


def automation_to_dict(
    engine: AutomationEngine,
) -> dict[str, Any]:
    """
    Convert persistence-safe automation to plain JSON-compatible structures.

    Code-defined callable aliases/triggers are deliberately skipped.
    """

    aliases: list[
        dict[str, Any]
    ] = []

    for alias in engine.aliases.values():
        if not isinstance(
            alias.expansion,
            str,
        ):
            continue

        aliases.append(
            {
                "pattern": alias.pattern,
                "expansion": alias.expansion,
                "enabled": alias.enabled,
            }
        )

    triggers: list[
        dict[str, Any]
    ] = []

    for trigger in engine.triggers.values():
        if trigger.response_template is None:
            continue

        triggers.append(
            {
                "pattern": trigger.pattern,
                "response": trigger.response_template,
                "gag": trigger.gag,
                "one_shot": trigger.one_shot,
                "cooldown_s": trigger.cooldown_s,
                "case_sensitive": trigger.case_sensitive,
                "priority": trigger.priority,
                "enabled": trigger.enabled,
                "style": _style_filter_to_dict(trigger.style_filter),
            }
        )

    return {
        "enabled": engine.enabled,
        "aliases": aliases,
        "triggers": triggers,
    }


def save_automation(
    engine: AutomationEngine,
    path: str = DEFAULT_AUTOMATION_PATH,
) -> None:
    data = automation_to_dict(
        engine
    )

    _atomic_write_document(
        path,
        kind=PERSISTENCE_KIND_AUTOMATION,
        payload=data,
    )


# ---------------------------------------------------------------------------
# Automation loading
# ---------------------------------------------------------------------------


def _validate_automation_data(
    data: Any,
    *,
    path: str,
) -> tuple[
    bool,
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """
    Validate the entire automation document before touching the live engine.

    This prevents a malformed entry halfway through the file from leaving the
    engine partially loaded.
    """

    if not isinstance(
        data,
        dict,
    ):
        raise ValueError(
            f"{path}: expected a JSON object"
        )

    unknown_top_level = set(
        data
    ) - {
        "enabled",
        "aliases",
        "triggers",
    }

    if unknown_top_level:
        raise ValueError(
            f"{path}: unknown top-level field(s): "
            f"{', '.join(sorted(unknown_top_level))}"
        )

    master_enabled = _require_bool(
        data.get("enabled", True),
        field=f"{path}.enabled",
    )

    raw_aliases = data.get(
        "aliases",
        [],
    )

    raw_triggers = data.get(
        "triggers",
        [],
    )

    if not isinstance(
        raw_aliases,
        list,
    ):
        raise ValueError(
            f"{path}: 'aliases' must be an array"
        )

    if not isinstance(
        raw_triggers,
        list,
    ):
        raise ValueError(
            f"{path}: 'triggers' must be an array"
        )

    if len(raw_aliases) > MAX_ALIASES:
        raise ValueError(
            f"{path}: too many aliases "
            f"(maximum {MAX_ALIASES})"
        )

    if len(raw_triggers) > MAX_TRIGGERS:
        raise ValueError(
            f"{path}: too many triggers "
            f"(maximum {MAX_TRIGGERS})"
        )

    aliases: list[
        dict[str, Any]
    ] = []

    for index, entry in enumerate(
        raw_aliases
    ):
        label = (
            f"{path}: aliases[{index}]"
        )

        if not isinstance(
            entry,
            dict,
        ):
            raise ValueError(
                f"{label}: expected object"
            )

        unknown = set(
            entry
        ) - {
            "pattern",
            "expansion",
            "enabled",
        }

        if unknown:
            raise ValueError(
                f"{label}: unknown field(s): "
                f"{', '.join(sorted(unknown))}"
            )

        if "pattern" not in entry:
            raise ValueError(
                f"{label}: missing 'pattern'"
            )

        if "expansion" not in entry:
            raise ValueError(
                f"{label}: missing 'expansion'"
            )

        pattern = _require_string(
            entry["pattern"],
            field=f"{label}.pattern",
        )

        expansion = _require_string(
            entry["expansion"],
            field=f"{label}.expansion",
        )

        enabled = _require_bool(
            entry.get(
                "enabled",
                True,
            ),
            field=f"{label}.enabled",
        )

        aliases.append(
            {
                "pattern": pattern,
                "expansion": expansion,
                "enabled": enabled,
            }
        )

    triggers: list[
        dict[str, Any]
    ] = []

    for index, entry in enumerate(
        raw_triggers
    ):
        label = (
            f"{path}: triggers[{index}]"
        )

        if not isinstance(
            entry,
            dict,
        ):
            raise ValueError(
                f"{label}: expected object"
            )

        unknown = set(
            entry
        ) - {
            "pattern",
            "response",
            "gag",
            "one_shot",
            "cooldown_s",
            "case_sensitive",
            "priority",
            "enabled",
            "style",
        }

        if unknown:
            raise ValueError(
                f"{label}: unknown field(s): "
                f"{', '.join(sorted(unknown))}"
            )

        if "pattern" not in entry:
            raise ValueError(
                f"{label}: missing 'pattern'"
            )

        if "response" not in entry:
            raise ValueError(
                f"{label}: missing 'response'"
            )

        pattern = _require_string(
            entry["pattern"],
            field=f"{label}.pattern",
        )

        response = _require_string(
            entry["response"],
            field=f"{label}.response",
        )

        gag = _require_bool(
            entry.get(
                "gag",
                False,
            ),
            field=f"{label}.gag",
        )

        one_shot = _require_bool(
            entry.get(
                "one_shot",
                False,
            ),
            field=f"{label}.one_shot",
        )

        cooldown_s = (
            _require_nonnegative_finite_float(
                entry.get(
                    "cooldown_s",
                    0.0,
                ),
                field=f"{label}.cooldown_s",
            )
        )

        case_sensitive = _require_bool(
            entry.get(
                "case_sensitive",
                False,
            ),
            field=f"{label}.case_sensitive",
        )

        priority_value = entry.get("priority", 0)
        if isinstance(priority_value, bool):
            raise ValueError(f"{label}.priority: expected integer")
        try:
            priority = int(priority_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}.priority: expected integer") from exc
        if not -100000 <= priority <= 100000:
            raise ValueError(f"{label}.priority: must be between -100000 and 100000")

        enabled = _require_bool(
            entry.get(
                "enabled",
                True,
            ),
            field=f"{label}.enabled",
        )

        style_filter = _validate_style_filter(
            entry.get("style"),
            field=f"{label}.style",
        )

        # Validate regex now, before touching the live engine.
        try:
            import re

            re.compile(
                pattern,
                (
                    0
                    if case_sensitive
                    else re.IGNORECASE
                ),
            )

        except re.error as exc:
            raise ValueError(
                f"{label}.pattern: invalid regex: {exc}"
            ) from exc

        triggers.append(
            {
                "pattern": pattern,
                "response": response,
                "gag": gag,
                "one_shot": one_shot,
                "cooldown_s": cooldown_s,
                "case_sensitive": case_sensitive,
                "priority": priority,
                "enabled": enabled,
                "style_filter": style_filter,
            }
        )

    return (
        master_enabled,
        aliases,
        triggers,
    )


def load_automation(
    engine: AutomationEngine,
    path: str = DEFAULT_AUTOMATION_PATH,
) -> tuple[int, int]:
    """
    Load persistence-safe aliases and triggers.

    Returns:

        (aliases_loaded, triggers_loaded)

    Missing file returns:

        (0, 0)

    The entire file is validated before the engine is mutated.
    """

    data = _read_json_file(
        path
    )

    if data is None:
        return (
            0,
            0,
        )

    _version, data = _unwrap_document(
        data, path=path, kind=PERSISTENCE_KIND_AUTOMATION
    )

    master_enabled, aliases, triggers = (
        _validate_automation_data(
            data,
            path=path,
        )
    )

    # Apply only after full validation succeeds.
    #
    # We intentionally ADD entries rather than clearing the engine because
    # callers may already have code-defined automation registered.
    # Persistence-safe entries loaded here are new objects.
    alias_ids: list[str] = []
    trigger_ids: list[str] = []

    try:
        for entry in aliases:
            alias_id = engine.add_alias(
                entry["pattern"],
                entry["expansion"],
            )

            alias_ids.append(
                alias_id
            )

            if not entry["enabled"]:
                engine.set_enabled(
                    "alias",
                    alias_id,
                    False,
                )

        for entry in triggers:
            trigger_id = (
                engine.add_simple_trigger(
                    entry["pattern"],
                    entry["response"],
                    gag=entry["gag"],
                    one_shot=entry["one_shot"],
                    cooldown_s=entry["cooldown_s"],
                    case_sensitive=entry["case_sensitive"],
                    priority=entry["priority"],
                    style_filter=entry["style_filter"],
                )
            )

            trigger_ids.append(
                trigger_id
            )

            if not entry["enabled"]:
                engine.set_enabled(
                    "trigger",
                    trigger_id,
                    False,
                )

    except Exception:
        # Roll back only entries created during this load operation.
        for alias_id in alias_ids:
            engine.remove(
                "alias",
                alias_id,
            )

        for trigger_id in trigger_ids:
            engine.remove(
                "trigger",
                trigger_id,
            )

        raise

    engine.set_master_enabled(master_enabled)

    return (
        len(alias_ids),
        len(trigger_ids),
    )


# ---------------------------------------------------------------------------
# Numpad macro persistence
# ---------------------------------------------------------------------------

_MACRO_LAYERS = ("base", "ctrl", "alt")
_MACRO_KEYS = ("7", "8", "9", "divide", "4", "5", "6", "multiply",
               "1", "2", "3", "subtract", "0", "decimal", "add", "enter")


def default_macro_config() -> dict[str, Any]:
    return {
        "enabled": True,
        "bindings": {layer: {} for layer in _MACRO_LAYERS},
    }


def _validate_macro_data(data: Any, *, path: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected macro configuration object")

    unknown = set(data) - {"enabled", "bindings"}
    if unknown:
        raise ValueError(f"{path}: unknown field(s): {', '.join(sorted(unknown))}")

    enabled = _require_bool(data.get("enabled", True), field=f"{path}.enabled")
    raw_bindings = data.get("bindings", {})
    if not isinstance(raw_bindings, dict):
        raise ValueError(f"{path}: bindings must be an object")

    unknown_layers = set(raw_bindings) - set(_MACRO_LAYERS)
    if unknown_layers:
        raise ValueError(
            f"{path}: unknown macro layer(s): {', '.join(sorted(unknown_layers))}"
        )

    bindings: dict[str, dict[str, dict[str, Any]]] = {
        layer: {} for layer in _MACRO_LAYERS
    }
    for layer in _MACRO_LAYERS:
        raw_layer = raw_bindings.get(layer, {})
        if not isinstance(raw_layer, dict):
            raise ValueError(f"{path}: bindings.{layer} must be an object")
        unknown_keys = set(raw_layer) - set(_MACRO_KEYS)
        if unknown_keys:
            raise ValueError(
                f"{path}: unknown numpad key(s) in {layer}: "
                f"{', '.join(sorted(unknown_keys))}"
            )
        for key, raw in raw_layer.items():
            label = f"{path}: bindings.{layer}.{key}"
            if not isinstance(raw, dict):
                raise ValueError(f"{label}: expected object")
            unknown_fields = set(raw) - {"label", "command", "enabled"}
            if unknown_fields:
                raise ValueError(
                    f"{label}: unknown field(s): "
                    f"{', '.join(sorted(unknown_fields))}"
                )
            command = _require_string(
                raw.get("command", ""), field=f"{label}.command"
            )
            display = _require_string(
                raw.get("label", ""), field=f"{label}.label"
            )
            item_enabled = _require_bool(
                raw.get("enabled", True), field=f"{label}.enabled"
            )
            bindings[layer][key] = {
                "label": display,
                "command": command,
                "enabled": item_enabled,
            }

    return {"enabled": enabled, "bindings": bindings}


def load_macros(path: str = DEFAULT_MACROS_PATH) -> dict[str, Any]:
    data = _read_json_file(path)
    if data is None:
        return default_macro_config()
    _version, data = _unwrap_document(
        data, path=path, kind=PERSISTENCE_KIND_MACROS
    )
    return _validate_macro_data(data, path=path)


def save_macros(config: dict[str, Any], path: str = DEFAULT_MACROS_PATH) -> None:
    normalized = _validate_macro_data(config, path=path)
    _atomic_write_document(
        path, kind=PERSISTENCE_KIND_MACROS, payload=normalized
    )


# ---------------------------------------------------------------------------
# Typed client variable persistence
# ---------------------------------------------------------------------------


def _validate_variable_data(data: Any, *, path: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected variable object")
    if len(data) > MAX_VARIABLES:
        raise ValueError(f"{path}: too many variables (maximum {MAX_VARIABLES})")

    normalized: dict[str, Any] = {}
    for name, value in data.items():
        if not isinstance(name, str):
            raise ValueError(f"{path}: variable names must be strings")
        try:
            clean_name = validate_variable_name(name)
            clean_value = validate_variable_value(value)
        except ValueError as exc:
            raise ValueError(f"{path}: variable {name!r}: {exc}") from exc
        normalized[clean_name] = clean_value
    return normalized


def load_variables(path: str = DEFAULT_VARIABLES_PATH) -> VariableStore:
    data = _read_json_file(path)
    if data is None:
        return VariableStore()
    _version, payload = _unwrap_document(
        data, path=path, kind=PERSISTENCE_KIND_VARIABLES
    )
    return VariableStore(_validate_variable_data(payload, path=path))


def load_variables_safely(
    path: str = DEFAULT_VARIABLES_PATH,
) -> tuple[VariableStore, str | None]:
    try:
        return load_variables(path), None
    except (ValueError, OSError) as exc:
        return VariableStore(), str(exc)


def save_variables(
    variables: VariableStore | dict[str, Any],
    path: str = DEFAULT_VARIABLES_PATH,
) -> None:
    if isinstance(variables, VariableStore):
        raw = variables.as_dict()
    elif isinstance(variables, dict):
        raw = variables
    else:
        raise ValueError("variables must be a VariableStore or mapping")
    normalized = _validate_variable_data(raw, path=path)
    _atomic_write_document(
        path, kind=PERSISTENCE_KIND_VARIABLES, payload=normalized
    )


# ---------------------------------------------------------------------------
# Explicit schema migration helpers
# ---------------------------------------------------------------------------


def persistence_document_version(
    path: str | Path,
    *,
    kind: str,
) -> int | None:
    """Return the persisted schema version, ``0`` for legacy, or ``None`` if absent."""

    data = _read_json_file(path)
    if data is None:
        return None
    version, _payload = _unwrap_document(data, path=str(path), kind=kind)
    return version


def migrate_legacy_persistence(
    path: str | Path,
    *,
    kind: str,
) -> str | None:
    """Explicitly upgrade one validated legacy document to schema v1.

    Normal reads are intentionally side-effect free.  Ordinary saves also
    perform this migration automatically.  This helper exists for a future
    migration UI/startup step that wants to upgrade a file before the user
    otherwise changes it.

    Returns the backup path when a migration occurred, otherwise ``None``.
    """

    if kind not in {
        PERSISTENCE_KIND_PROFILES,
        PERSISTENCE_KIND_AUTOMATION,
        PERSISTENCE_KIND_MACROS,
        PERSISTENCE_KIND_VARIABLES,
    }:
        raise ValueError(f"unknown persistence kind {kind!r}")

    version = persistence_document_version(path, kind=kind)
    if version is None or version >= PERSISTENCE_SCHEMA_VERSION:
        return None

    path_str = str(path)
    if kind == PERSISTENCE_KIND_PROFILES:
        payload: Any = load_profiles(path_str)
    elif kind == PERSISTENCE_KIND_AUTOMATION:
        scratch = AutomationEngine(send_fn=lambda _line: None)
        load_automation(scratch, path_str)
        payload = automation_to_dict(scratch)
    elif kind == PERSISTENCE_KIND_MACROS:
        payload = load_macros(path_str)
    else:
        payload = load_variables(path_str).as_dict()

    backup = _ensure_pre_migration_backup(path, kind=kind)
    _atomic_write_json(path, _document_envelope(kind, payload))
    return backup


# ---------------------------------------------------------------------------
# Application settings
# ---------------------------------------------------------------------------


def load_settings(path: str | Path = DEFAULT_SETTINGS_PATH) -> ClientSettings:
    """Load global client settings from the common versioned envelope."""
    raw = _read_json_file(path)
    if raw is None:
        return ClientSettings()
    _version, payload = _unwrap_document(
        raw, path=str(path), kind=PERSISTENCE_KIND_SETTINGS
    )
    return ClientSettings.from_dict(payload)


def load_settings_safely(
    path: str | Path = DEFAULT_SETTINGS_PATH,
) -> tuple[ClientSettings, str | None]:
    """Fail-soft settings loader used during desktop startup."""
    try:
        return load_settings(path), None
    except (ValueError, OSError) as exc:
        return ClientSettings(), str(exc)


def save_settings(
    settings: ClientSettings,
    path: str | Path = DEFAULT_SETTINGS_PATH,
) -> None:
    """Validate and atomically persist global client settings."""
    if not isinstance(settings, ClientSettings):
        raise ValueError("settings must be a ClientSettings instance")
    validated = ClientSettings.from_dict(settings.to_dict())
    _atomic_write_document(
        path,
        kind=PERSISTENCE_KIND_SETTINGS,
        payload=validated.to_dict(),
    )
