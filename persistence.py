"""
persistence.py -- small JSON-backed save/load for connection profiles
and automation (aliases/triggers), so a session survives a restart
without editing app.py.

Deliberately NOT a database: two flat JSON files next to the client.
Triggers whose action is an arbitrary Python callable (defined in code,
like the ones in example_wire.py) can't be serialized -- only "simple"
triggers/aliases (pattern -> a response template string, built via
AutomationEngine.add_simple_trigger / add_alias) round-trip through
these files. That's a deliberate line: code-defined automation lives
in code; user-authored automation (via in-app slash commands) lives
in these files.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from automation import AutomationEngine

DEFAULT_PROFILES_PATH = "profiles.json"
DEFAULT_AUTOMATION_PATH = "automation.json"


# -- connection profiles --------------------------------------------------


def load_profiles(path: str = DEFAULT_PROFILES_PATH) -> dict[str, dict[str, Any]]:
    """Returns {name: {"host": ..., "port": ...}}. Missing file -> {}."""
    p = Path(path)
    if not p.exists():
        return {}
    with p.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object of profiles")
    return data


def save_profile(name: str, host: str, port: int, path: str = DEFAULT_PROFILES_PATH) -> None:
    profiles = load_profiles(path)
    profiles[name] = {"host": host, "port": port}
    with Path(path).open("w", encoding="utf-8") as f:
        json.dump(profiles, f, indent=2, sort_keys=True)


def resolve_connection(arg1: str | None, arg2: str | None, path: str = DEFAULT_PROFILES_PATH):
    """CLI-friendly resolution:
      - no args -> (None, None)
      - one arg that matches a saved profile name -> that profile's host/port
      - one arg that doesn't match a profile -> treated as a bare host, no port
      - two args -> (host, port) directly, ignoring any saved profiles
    Returns (host, port) with port as int or None.
    """
    if arg1 and arg2:
        return arg1, int(arg2)
    if arg1:
        profiles = load_profiles(path)
        if arg1 in profiles:
            prof = profiles[arg1]
            return prof["host"], int(prof["port"])
        return arg1, None
    return None, None


# -- automation (aliases / simple triggers) --------------------------------


def automation_to_dict(engine: AutomationEngine) -> dict[str, list[dict[str, Any]]]:
    aliases = []
    for alias in engine.aliases.values():
        if isinstance(alias.expansion, str):  # skip code-defined callable expansions
            aliases.append({"pattern": alias.pattern, "expansion": alias.expansion})

    triggers = []
    for trig in engine.triggers.values():
        if trig.response_template is not None:  # skip code-defined callable actions
            triggers.append(
                {
                    "pattern": trig.pattern,
                    "response": trig.response_template,
                    "gag": trig.gag,
                    "one_shot": trig.one_shot,
                    "cooldown_s": trig.cooldown_s,
                    "case_sensitive": trig.case_sensitive,
                }
            )
    return {"aliases": aliases, "triggers": triggers}


def save_automation(engine: AutomationEngine, path: str = DEFAULT_AUTOMATION_PATH) -> None:
    data = automation_to_dict(engine)
    with Path(path).open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)


def load_automation(engine: AutomationEngine, path: str = DEFAULT_AUTOMATION_PATH) -> tuple[int, int]:
    """Loads aliases/triggers from disk into engine. Returns (n_aliases,
    n_triggers) loaded; (0, 0) if the file doesn't exist yet."""
    p = Path(path)
    if not p.exists():
        return 0, 0
    with p.open("r", encoding="utf-8") as f:
        data = json.load(f)

    n_aliases = 0
    for entry in data.get("aliases", []):
        engine.add_alias(entry["pattern"], entry["expansion"])
        n_aliases += 1

    n_triggers = 0
    for entry in data.get("triggers", []):
        engine.add_simple_trigger(
            entry["pattern"],
            entry["response"],
            gag=entry.get("gag", False),
            one_shot=entry.get("one_shot", False),
            cooldown_s=entry.get("cooldown_s", 0.0),
            case_sensitive=entry.get("case_sensitive", False),
        )
        n_triggers += 1

    return n_aliases, n_triggers
