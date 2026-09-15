# Reference Audit: PyMUD, MudPyC, and QMud

The three reference repositories were used as architectural comparison material, not as code to transplant wholesale.

## ADOPT

### Explicit architecture invariants
QMud demonstrates the value of documenting thread/state/callback/lifecycle rules as contributor contracts. This project now records equivalent Python/qasync invariants in `../architecture/ARCHITECTURE_INVARIANTS.md` and backs key boundaries with tests.

### Bounded state everywhere
QMud and MudPyC reinforce the rule that event queues, parser buffers, retained protocol state, and histories need explicit ceilings. This matches the client's existing MCCP, Telnet SB, ANSI, GMCP/MSDP, event-log, and history hardening.

### Structured ownership discipline
MudPyC's Trio nurseries/cancellation scopes are not being copied, but their ownership model is adopted in Python/asyncio form: `TaskOwner` gives background work one cancellation/await owner, and closeable `Subscription` handles make event-listener lifetime explicit.

### Test taxonomy
QMud's separation of unit, integration, regression, GUI, and smoke tests is adopted in a Python-appropriate form under `tests/`.

## ADAPT

### Telnet renegotiation/churn protection
QMud explicitly protects against pathological negotiation loops. This has now been adapted as a bounded per-option rolling-window guard with temporary cooling-off, Protocol-event diagnostics, and state-based idempotence for duplicate accepted WILL/DO messages.

### Command-processing boundary
QMud's command system and MudPyC's Process/Command hierarchy both warn against letting aliases, macros, mapper walking, automation, and scripts call transport ad hoc. This has now been adapted as a lightweight typed `CommandPipeline` with fixed source policies; mapper/script sources are reserved without importing either project's heavier process framework.

### Mapper architecture
MudPyC validates a persistent room/exit graph, transaction-aware repository layer, pathfinding separate from UI, and MUD-specific adapters. The future mapper should use SQLite and small modules rather than a giant mapper class or JSON room dump.

### MUD-specific adapters
Protocol interpretation that is game-specific should live behind optional adapters instead of contaminating `client_core.py` or generic mapper code.

### Migration backups
Atomic saves prevent corruption; backups protect users from migration/user mistakes. Backup-on-migration belongs with the future coherent persistence schema layer.

## DO NOT COPY

### Complexity for compatibility's sake
QMud/MUSHclient's decades of compatibility requirements and multithreaded Lua machinery solve problems this client does not yet have. Do not import that complexity preemptively.

### Giant mapper modules
MudPyC's mature mapper is useful reference material, but its very large central module is a warning. Split model, repository, graph/pathfinding, walker, adapters, and Qt presentation early.

### Direct plugin access to mutable internals
Future extensions should receive snapshots/read APIs and submit explicit actions. They should not mutate widgets, socket writers, or arbitrary controller attributes directly.

### Simplified Telnet parsing from server examples
PyMUD's lightweight server-side Telnet handling is suitable to its scope but not a model for this client's native MCCP/GMCP/MSDP/GA/EOR needs.
