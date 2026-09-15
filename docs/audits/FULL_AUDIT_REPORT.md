# Full-System Stick Audit

This audit was performed after Hardening 5 with feature work frozen.  The goal
was to attack the current implementation across transport, parsing, session
lifecycle, persistence, automation, multi-session Qt integration, and features
that had already been live-tested in earlier builds.

## Outcome

No additional P0 issue was found in this sweep.  Several real P1/P2 defects
were found and fixed, including two cross-version regressions that the existing
test suite had failed to notice.

## Fixed findings

### P1 — automatic reconnect task could cancel itself

`_reconnect_after()` runs inside `self._reconnect_task` and calls `connect()`.
`connect()` then called `_cancel_reconnect_task()`, which cancelled the current
reconnect task itself.  Cancellation could be delivered at the next await and
abort the automatic reconnect.

Fix: `_cancel_reconnect_task()` now clears the stored reference but never
cancels `asyncio.current_task()`.  A regression test executes the real
`_reconnect_after() -> connect()` path with an asynchronous fake transport.

### P1 — cancelled transport shutdown could leave poisoned lifecycle state

`MudConnection.disconnect()` previously had no outer `finally`.  Cancelling the
caller while it awaited `writer.wait_closed()` could leave `_disconnecting`
true and reader/writer/task references stale.

Fix: transport teardown state cleanup and the one-shot DISCONNECTED emission
now run from `finally`, while caller cancellation still propagates.

### P1 — deeply nested persisted JSON escaped fail-soft startup policy

The persistence size bound prevented very large files but a small, extremely
deep JSON document can raise `RecursionError` inside the JSON decoder before
schema validation.

Fix: `_read_json_file()` normalizes decoder `RecursionError` to `ValueError`,
which is already handled by the application-facing fail-soft loaders.  The
source file remains untouched.

### P1 — malformed compact `#trigger` command could escape into the UI

The GUI trigger editor validated regexes, but the compact command path still
called `add_simple_trigger()` without handling an invalid regex/non-finite
cooldown validation error.

Fix: the command path reports `[could not add trigger: ...]` and leaves the
engine unchanged.

### P1 — MSDP snapshot regression

`client_core` emits one MSDP item as:

```text
{"variable": "HEALTH", "value": "100"}
```

A later controller version had regressed to storing literal snapshot keys
`variable` and `value`.  An existing test hid the problem by feeding a shape
that the real transport does not emit.

Fix: the controller again stores `protocol.msdp[variable] = value`, with a
compatibility fallback for already-flattened mappings.  The test now uses the
real transport event contract.

### P1 — protocol inspector/status functionality regressed out of later builds

The live-tested richer inspector was lost during later branch/package work:

- named Telnet option table
- Events tab
- bounded recent protocol event feed
- TELNET / GMCP / MSDP status counts
- terminal-type status field
- connection action enable/disable state

The README still claimed the event feed, so source and documented behavior had
diverged.

Fix: restored the known-good functionality and added structural regression
coverage.  The restored event feed is hardened: 200 events maximum and large
single event payloads are truncated to a 4096-character diagnostic summary so
history cannot retain tens of megabytes of protocol data.

### P2 — tab close could destroy Qt objects before controller teardown

A session tab scheduled `controller.close()` and immediately called
`deleteLater()` on its widget.  Final controller emissions could race the Qt
bridge's destruction.

Fix: `_close_session_widget()` now awaits controller shutdown before scheduling
the widget for deletion.

### P2 — session listener exception could interrupt controller work

A presentation/plugin listener registered through `MudSessionController.on()`
could raise through `_emit()`.

Fix: listener failures are logged and isolated so one observer cannot stop
other observers or abort session processing.

### P2 — in-progress command-history draft restoration had regressed

The earlier behavior that preserved partially typed input while browsing
history was no longer present.

Fix: when history browsing begins at the newest position, the current command
entry is saved as a draft and restored when Down returns to the end.

## Additional abuse testing

A deterministic smoke/fuzz pass exercised 5,000 random split Telnet byte
streams and 5,000 random split ANSI/control strings.  Protocol-limit exceptions
were treated as expected rejections.  No additional unexpected parser
exception was observed after providing the parser with the connected-writer
precondition used in production.

## Regression status

- 100 tests pass.
- All Python sources compile.
- Tests now explicitly cover the production automatic-reconnect path, cancelled
  transport teardown, deep JSON fail-soft handling, compact bad-regex handling,
  real MSDP event shape, bounded protocol history, protocol-inspector presence,
  tab-close ordering, listener isolation, and history-draft restoration.

## Remaining known risks / deferred work

These are not hidden release claims; they remain intentional follow-up items.

### P2 — regex denial-of-service potential

Triggers use Python's standard `re` engine.  A user-authored pathological regex
can exhibit catastrophic backtracking when applied to server-controlled text.
Input line size is bounded, but Python `re` has no built-in execution timeout.
Addressing this cleanly likely requires either a different regex engine,
process/thread isolation with cancellation semantics, or intentionally
restricting supported regex constructs.

### P2 — persistence schema versioning

Profiles, automation, and macros still use legacy unversioned document shapes.
A coherent version/migration layer remains the next planned persistence
milestone rather than adding incompatible markers piecemeal.

### P2 — GUI-runtime coverage in this build environment

Qt-specific source structure is regression-tested, while full interactive Qt
behavior still receives its strongest validation on the Windows/PySide6 machine
used for live testing.  The UI/backend boundaries remain deliberately thin to
keep most behavior directly unit-testable without Qt.

## Recommendation

This is a reasonable point to end broad hardening sweeps.  Future hardening
should be driven by a concrete finding, live interoperability issue, fuzzing
result, or review observation.  The next planned development milestone can move
to persistence schema/versioning and the settings foundation without pretending
that software can ever be proven bug-free.
