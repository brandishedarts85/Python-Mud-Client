# Variables 1 Report

## Scope

Variables 1 adds one typed, bounded variable subsystem without introducing a
scripting language or mapper-specific state.

## Model

`variables.py` is UI-neutral and provides `VariableStore` plus strict name/value
validation. Supported values are strings, integers, finite floats, and booleans.
The store is bounded to 1000 entries; string values are bounded to 64 KiB.

Substitution syntax is `${name}`. Unknown names remain literal. This avoids a
typo silently deleting command text.

## Command pipeline integration

Variable substitution happens inside `CommandPipeline` after alias expansion.
That gives every existing and reserved command source the same semantics:
manual input, numpad macros, automation responses, mapper, and scripts. Client
`#` commands are handled before substitution, so variable contents cannot turn
ordinary text into a privileged local command.

Telnet `WILL ECHO` remains authoritative for local-echo suppression; variable
support does not expose password text through local scrollback.

## Scope and persistence

Unnamed sessions use `variables.json`. Saved connection profiles use stable
hashed files under `profiles_data/`, matching automation/macro scope behavior.
Variable documents use the common v1 persistence envelope and legacy v0
migration/backup contract.

Every UI/command mutation persists atomically. If persistence fails, live state
is rolled back to its previous value. Future schema versions are rejected rather
than overwritten.

## UI and commands

A dockable Variables panel shows Name / Type / Value and supports add, edit,
rename, and remove. Its controller subscription has explicit teardown when the
active session changes.

Client commands:

```text
#set NAME[:TYPE] = VALUE
#unset NAME
#vars
#savevars
```

The default type is string. Explicit types are `string`, `int`, `float`, and
`bool`.

## Validation

The final milestone validation covers typed substitution, alias/automation/macro
paths, profile isolation, persistence migration, future-version refusal, failed
save rollback, duplicate-name rename protection, architecture boundaries, and
existing regressions.
