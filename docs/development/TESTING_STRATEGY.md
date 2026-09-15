# Testing Strategy

Tests are organized by the failure boundary they protect.

- `tests/unit/` — deterministic single-module behavior with no transport/UI integration.
- `tests/protocol/` — Telnet, MCCP, GMCP/MSDP, UTF-8, ANSI, and parser boundary behavior.
- `tests/integration/` — contracts spanning production modules, especially transport/controller/automation interactions.
- `tests/regression/` — minimal reproductions of defects that previously escaped review or returned in later builds.
- `tests/qt/` — Qt structure/startup/bridge/UI lifecycle behavior. Prefer deterministic signal/state checks over sleeps.
- `tests/fuzz/` — deterministic seeded parser/protocol abuse tests. Randomized failures must print/save the seed and become ordinary regression tests when fixed.

## Rules

1. A regression test must exercise the same data shape and path as production. Do not mock a convenient shape that the real lower layer never emits.
2. No test should depend on a public MUD or external network service.
3. Time-based tests use controllable clocks/events where possible; arbitrary sleeps are a last resort.
4. Parser tests include recovery: valid input after malformed input must still work.
5. Lifecycle tests assert terminal state and resource cleanup, not merely that no exception was printed.
6. Command-boundary tests exercise each producer policy (manual, macro, automation, and future mapper/script defaults), including Telnet ECHO safety.
7. Architecture tests prevent session code from creating unowned tasks or sending transport commands outside the controlled boundary.
8. New feature work includes tests at the narrowest useful layer plus integration/regression coverage when a boundary is involved.
