"""
persistence.py -- small JSON-backed save/load for connection profiles
and user-authored automation.

This is intentionally not a database.

Two flat JSON files are used by default:

    profiles.json
    automation.json

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

import json
import math
import os
import tempfile

from pathlib import Path
from typing import Any

from automation import AutomationEngine


DEFAULT_PROFILES_PATH = "profiles.json"
DEFAULT_AUTOMATION_PATH = "automation.json"

MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_PROFILES = 1000
MAX_ALIASES = 10_000
MAX_TRIGGERS = 10_000


# ---------------------------------------------------------------------------
# Generic JSON helpers
# ---------------------------------------------------------------------------


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
            return json.load(file)

    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{p}: invalid JSON at line "
            f"{exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc

    except OSError as exc:
        raise ValueError(
            f"{p}: unable to read file: {exc}"
        ) from exc


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

        profiles[
            name
        ] = {
            "host": host,
            "port": port,
        }

    return profiles


def save_profile(
    name: str,
    host: str,
    port: int,
    path: str = DEFAULT_PROFILES_PATH,
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

    profiles = load_profiles(
        path
    )

    profiles[
        name
    ] = {
        "host": host,
        "port": port,
    }

    if len(profiles) > MAX_PROFILES:
        raise ValueError(
            f"too many profiles "
            f"(maximum {MAX_PROFILES})"
        )

    _atomic_write_json(
        path,
        profiles,
    )


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


def automation_to_dict(
    engine: AutomationEngine,
) -> dict[str, list[dict[str, Any]]]:
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
                "enabled": trigger.enabled,
            }
        )

    return {
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

    _atomic_write_json(
        path,
        data,
    )


# ---------------------------------------------------------------------------
# Automation loading
# ---------------------------------------------------------------------------


def _validate_automation_data(
    data: Any,
    *,
    path: str,
) -> tuple[
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
        "aliases",
        "triggers",
    }

    if unknown_top_level:
        raise ValueError(
            f"{path}: unknown top-level field(s): "
            f"{', '.join(sorted(unknown_top_level))}"
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

        enabled = _require_bool(
            entry.get(
                "enabled",
                True,
            ),
            field=f"{label}.enabled",
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
                "enabled": enabled,
            }
        )

    return (
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

    aliases, triggers = (
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

    return (
        len(alias_ids),
        len(trigger_ids),
    )
