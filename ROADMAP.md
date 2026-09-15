# Python MUD Client Roadmap

This file is the quick, user-facing view of what the client can do today and
what is planned next.  It intentionally stays higher level than the engineering
reports in this repository.

## Status key

- `[x]` Available now
- `[~]` Foundation exists, more work planned
- `[ ]` Planned
- `[-]` Intentionally deferred / not a 1.0 requirement

## Current 1.0 feature set

### Desktop and sessions

- [x] Native PySide6 / Qt Widgets desktop UI
- [x] Multiple simultaneous MUD sessions in tabs
- [x] Movable, floatable, tab-able dock panels
- [x] Persistent window geometry and dock layout
- [x] Connect / disconnect / reconnect controls
- [x] Saved connection profiles
- [x] Per-session command history with draft restoration
- [x] Global Settings dialog with safe live appearance preview

### Telnet and protocol support

- [x] Telnet negotiation with bounded parser state
- [x] MCCP2 compression
- [x] GMCP
- [x] MSDP
- [x] NAWS
- [x] TTYPE
- [x] EOR / prompt-boundary handling
- [x] Server ECHO awareness for password-safe local echo
- [x] Telnet renegotiation-loop/churn protection
- [x] Protocol inspector with bounded event history
- [ ] CHARSET negotiation
- [ ] TLS / secure connection options

### Output, search, and logging

- [x] ANSI 16-color, xterm-256, and truecolor output
- [x] Bounded scrollback
- [x] Configurable output font and default colors
- [x] Optional visible timestamps
- [x] `Ctrl+F` transcript search with next/previous and case matching
- [x] Atomic transcript export
- [x] Per-session UTF-8 logging
- [x] Bounded log rotation

### Automation and input

- [x] Aliases
- [x] Regex triggers
- [x] ANSI/style-aware triggers
- [x] Trigger creation from selected output
- [x] Timers
- [x] Trigger priorities, cooldowns, and one-shot behavior
- [x] Automation test/explain support
- [x] Bounded automation firing log
- [x] Physical numpad macros with Base / Ctrl / Alt layers
- [x] Profile-scoped automation and macros
- [x] Shared outbound command pipeline
- [~] Higher-level process/command sequencing (mapper walker is first consumer)

### Variables

- [x] Typed variables: string, int, float, bool
- [x] `${name}` outbound substitution
- [x] Variables dock
- [x] `#set`, `#unset`, `#vars`, and `#savevars`
- [x] Profile-scoped persistence
- [ ] Trigger capture groups / temporary variables
- [ ] Expression support

### Mapper

- [x] Profile-scoped SQLite map database
- [x] Rooms, areas, coordinates, notes, and directed exits
- [x] Weighted shortest-path routing
- [x] Manual room and exit editing
- [x] Stable external room identity
- [x] Generic GMCP `Room.Info` adapter seam
- [x] Placeholder-room upgrade when identity becomes known
- [x] Prompt-aware one-command-at-a-time route walking
- [x] Automatic stop on timeout, disconnect, scope change, or divergence
- [x] Door exits with prompt-synchronized pre-commands
- [x] Special exits with arbitrary movement commands
- [x] Controlled rerouting from a positively observed unexpected room
- [x] Small reroute budget to prevent looping recovery
- [~] MUD-specific room adapters (adapter framework + evidenced MG.room adapter available; Discworld/Aurora still pending evidence)
- [ ] Persisted per-profile adapter selection
- [ ] Door-state / lock-state observation where a MUD exposes it
- [x] Graphical map rendering and interactive layout
- [x] Route preferences: avoid areas, avoid exit tags, prefer exit tags

## Current release: Mapper 6 — Route Preferences & Map Organization

This release makes the mapper easier to organize and gives routing explicit,
profile-scoped policy:

- exit tags are persisted in the mapper database;
- avoided exit tags are hard route constraints;
- avoided areas are skipped as intermediate route areas;
- preferred exit tags lower effective pathfinding cost without changing stored base cost;
- the same route policy is used for initial walking and controlled rerouting;
- the mapper dock can filter the table and visual map by area;
- room nodes can be dragged to save a manual visual layout without overwriting world X/Y/Z coordinates;
- manual layout can be cleared per room to return to coordinate/fallback layout;
- mapper schema v4 migrates older databases in place.

Camera pan/zoom state remains session-local for now. Manual room layout is persisted
because it represents map organization; camera position does not yet have a proven
profile-persistence requirement.

## Planned next

### More MUD adapters

- [ ] Capture live protocol evidence from Discworld and/or Aurora
- [ ] Add game-specific adapters only from captured/documented shapes
- [ ] Persist adapter choice per profile after selection semantics are proven
- [ ] Add door/lock-state observations where a protocol explicitly exposes them

### Mapper 7 — map organization polish

- [ ] Area management/renaming tools instead of room-by-room edits
- [ ] Bulk room tagging / notes workflows
- [ ] Optional route-policy presets
- [ ] Better special-exit/door legends and visual filtering
- [ ] Revisit whether camera pan/zoom should persist per profile after real use

### Plugin / scripting foundation

- [ ] Define snapshot-in / action-out API
- [ ] No direct mutable access to Qt widgets, transport, or controller internals
- [ ] Explicit subscription and task lifetime ownership
- [ ] Bounded queues / output where applicable
- [ ] Decide scripting runtime only after the API contract is proven

### 1.0 release candidate work

- [ ] Fresh-install desktop test
- [ ] Upgrade/migration test from historical configuration files
- [ ] Multi-session soak/stress test
- [ ] Live interoperability testing against several MUDs
- [ ] Run all PySide6 runtime tests on Windows desktop environment
- [ ] Packaging / executable distribution decision
- [ ] User documentation and keyboard-shortcut reference
- [ ] Final regression and adversarial parser sweep

## Later / optional work

These are useful but are not allowed to destabilize the 1.0 foundation:

- [-] Rich embedded scripting language
- [-] Plugin marketplace/package manager
- [-] MXP / MSP
- [-] Speech / text-to-speech
- [-] Advanced mapper terrain rendering
- [-] Cloud synchronization

## Engineering rules that govern the roadmap

New features should continue to obey the project invariants:

1. Qt does not own transport or protocol state.
2. Every background task and subscription has an explicit owner/lifetime.
3. Remote input is bounded before expensive processing.
4. Persisted state is validated and written atomically.
5. One broken session must not poison another session.
6. Automation and mapper actions go through the shared command pipeline.
7. Mapper automation advances only from trustworthy acknowledgements, not
   guesses from arbitrary text.
8. MUD-specific behavior belongs in adapters, not `client_core.py`.

See `docs/architecture/ARCHITECTURE_INVARIANTS.md` for the stricter engineering contract.
