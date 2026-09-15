# Victory Lap

This track applies lessons from the full-system stick audit and the PyMUD, MudPyC, and QMud reference audits without importing their complexity blindly.

## First Base — Architecture contract and test taxonomy — COMPLETE

- Added `../architecture/ARCHITECTURE_INVARIANTS.md`.
- Added `../audits/REFERENCE_AUDIT.md` with ADOPT / ADAPT / DO NOT COPY decisions.
- Added `../development/TESTING_STRATEGY.md`.
- Reorganized tests into unit, protocol, integration, regression, Qt, and fuzz categories.
- Added executable checks preventing backend/controller dependencies on Qt/PySide6.
- Baseline after reorganization: 103 passing tests.

## Second Base — Protocol maturity — COMPLETE

- Added bounded per-option Telnet renegotiation/churn protection inspired by QMud.
- Duplicate accepted WILL/DO messages are idempotent and are not re-acknowledged.
- Legitimate disable/re-enable negotiation remains supported.
- Churn suppression affects only the offending option and expires automatically.
- Suppression produces one bounded diagnostic through the Protocol Events path without mutating negotiated option state.
- Added protocol and integration regressions for duplicate negotiation, legitimate renegotiation, suppression scope, expiry, reset, and inspector state.
- Baseline after second base: 110 passing tests.

## Third Base — Ownership and command boundary — COMPLETE

- Added UI-neutral `TaskOwner` and idempotent `Subscription` handles.
- Session tick/reconnect/client-command tasks now have one deterministic cancellation/await owner.
- Qt window async operations are window-owned; application shutdown retires them before controller teardown.
- `EventBus.on()` and `MudSessionController.on()` now return explicit subscription handles.
- Qt session bridge subscriptions are disposed before widget destruction.
- Automation dock now retains/closes its active-controller subscription, including the legacy engine-switch path.
- Legacy `AutomationEngine.attach()` gained an explicit `detach()` path.
- Added typed `CommandRequest` / `CommandSource` / `CommandPipeline` boundary.
- Manual and macro commands preserve alias/history/client-command semantics.
- Trigger/timer automation bypasses alias recursion/history and cannot invoke client commands, preserving prior behavior.
- Future mapper/script sources default to the same non-recursive safe policy.
- Telnet `WILL ECHO` remains the single authority for local command echo/password safety.
- Architecture tests prevent unowned session `create_task()` calls and transport sends outside the boundary helper.
- Baseline after third base: 127 passing tests.

## Home Plate — Persistence evolution and final audit — COMPLETE

- Added one v1 persistence envelope shared by profiles, automation, and macros.
- Historical unversioned documents are treated as legacy v0 and remain readable.
- Reads are side-effect free; the first successful save performs the v0→v1 migration.
- Legacy source bytes are preserved once as `.pre-v1.bak` before replacement.
- Added an explicit validated migration helper for a future migration UI/startup step.
- Unsupported future schema versions are rejected and cannot be overwritten by this client.
- Macro writes now use the same strict schema validator as macro reads.
- Preserved atomic whole-file validation, atomic replacement, fail-soft startup loading, and source recovery.
- Added migration/failure-injection regression coverage and reran the full invariant/protocol/Qt/fuzz gates.
- Final victory-lap baseline: 139 passing tests before the randomized smoke gate.
