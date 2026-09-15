from __future__ import annotations

import math

import pytest

from variables import VariableStore, parse_typed_value


def test_typed_variable_store_and_substitution():
    store = VariableStore({
        "target": "goblin",
        "count": 3,
        "ratio": 1.5,
        "enabled": True,
    })

    assert store.substitute(
        "say ${target} ${count} ${ratio} ${enabled} ${missing}"
    ) == "say goblin 3 1.5 true ${missing}"


def test_variable_names_and_values_are_bounded_and_validated():
    store = VariableStore()
    with pytest.raises(ValueError, match="variable name"):
        store.set("bad name", "x")
    with pytest.raises(ValueError, match="finite"):
        store.set("value", math.inf)
    with pytest.raises(ValueError, match="must be a string"):
        store.set("value", ["not", "scalar"])


def test_parse_typed_value_is_explicit_and_predictable():
    assert parse_typed_value("string", "001") == "001"
    assert parse_typed_value("int", "001") == 1
    assert parse_typed_value("float", "2.5") == 2.5
    assert parse_typed_value("bool", "yes") is True
    assert parse_typed_value("bool", "off") is False
    with pytest.raises(ValueError, match="expected true/false"):
        parse_typed_value("bool", "maybe")


def test_unknown_variable_reference_is_left_literal():
    store = VariableStore({"known": "ok"})
    assert store.substitute("${known}/${unknown}") == "ok/${unknown}"
