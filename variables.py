"""Typed, profile-aware client variables.

Variables are deliberately UI- and transport-independent.  They provide one
small substitution syntax for outbound commands::

    ${name}

Unknown variables are left unchanged so a typo cannot silently erase part of a
command.  Values are restricted to JSON scalar types used by the persistence
layer: string, integer, finite float, and boolean.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


VariableValue = str | int | float | bool

MAX_VARIABLES = 1000
MAX_VARIABLE_NAME_CHARS = 64
MAX_VARIABLE_STRING_CHARS = 64 * 1024

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_SUBSTITUTION_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_.-]{0,63})\}")


def validate_variable_name(name: str) -> str:
    if not isinstance(name, str):
        raise ValueError("variable name must be a string")
    if not _NAME_RE.fullmatch(name):
        raise ValueError(
            "variable name must start with a letter or underscore and contain "
            "only letters, digits, '_', '.', or '-' (maximum 64 characters)"
        )
    return name


def validate_variable_value(value: Any) -> VariableValue:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("variable float value must be finite")
        return value
    if isinstance(value, str):
        if len(value) > MAX_VARIABLE_STRING_CHARS:
            raise ValueError(
                f"variable string value exceeds {MAX_VARIABLE_STRING_CHARS} characters"
            )
        return value
    raise ValueError("variable value must be a string, integer, finite float, or boolean")


def variable_value_to_text(value: VariableValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


@dataclass(frozen=True)
class VariableEntry:
    name: str
    value: VariableValue

    @property
    def type_name(self) -> str:
        if isinstance(self.value, bool):
            return "bool"
        if isinstance(self.value, int):
            return "int"
        if isinstance(self.value, float):
            return "float"
        return "string"


class VariableStore:
    """Bounded typed variable mapping with deterministic substitution."""

    def __init__(self, initial: Mapping[str, VariableValue] | None = None) -> None:
        self._values: dict[str, VariableValue] = {}
        if initial:
            self.replace_all(initial)

    def __len__(self) -> int:
        return len(self._values)

    def __contains__(self, name: object) -> bool:
        return name in self._values

    def get(self, name: str, default: VariableValue | None = None):
        return self._values.get(name, default)

    def items(self) -> tuple[VariableEntry, ...]:
        return tuple(VariableEntry(name, value) for name, value in self._values.items())

    def as_dict(self) -> dict[str, VariableValue]:
        return dict(self._values)

    def set(self, name: str, value: Any) -> None:
        name = validate_variable_name(name)
        normalized = validate_variable_value(value)
        if name not in self._values and len(self._values) >= MAX_VARIABLES:
            raise ValueError(f"too many variables (maximum {MAX_VARIABLES})")
        self._values[name] = normalized

    def remove(self, name: str) -> bool:
        name = validate_variable_name(name)
        if name not in self._values:
            return False
        del self._values[name]
        return True

    def replace_all(self, values: Mapping[str, Any]) -> None:
        if len(values) > MAX_VARIABLES:
            raise ValueError(f"too many variables (maximum {MAX_VARIABLES})")
        normalized: dict[str, VariableValue] = {}
        for name, value in values.items():
            clean_name = validate_variable_name(name)
            normalized[clean_name] = validate_variable_value(value)
        self._values = normalized

    def substitute(self, text: str) -> str:
        if not isinstance(text, str):
            raise ValueError("variable substitution input must be a string")

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in self._values:
                return match.group(0)
            return variable_value_to_text(self._values[name])

        return _SUBSTITUTION_RE.sub(replace, text)


def parse_typed_value(type_name: str, text: str) -> VariableValue:
    kind = type_name.strip().lower()
    if kind in ("str", "string", "text"):
        return validate_variable_value(text)
    if kind in ("int", "integer"):
        try:
            value = int(text.strip(), 10)
        except ValueError as exc:
            raise ValueError("expected an integer") from exc
        return validate_variable_value(value)
    if kind in ("float", "number"):
        try:
            value = float(text.strip())
        except ValueError as exc:
            raise ValueError("expected a number") from exc
        return validate_variable_value(value)
    if kind in ("bool", "boolean"):
        lowered = text.strip().lower()
        if lowered in ("true", "yes", "on", "1"):
            return True
        if lowered in ("false", "no", "off", "0"):
            return False
        raise ValueError("expected true/false, yes/no, on/off, or 1/0")
    raise ValueError("variable type must be string, int, float, or bool")
