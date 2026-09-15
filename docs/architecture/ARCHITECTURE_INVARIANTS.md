# Architecture Invariants

These rules are part of the client contract, not implementation suggestions. New features must preserve them. Tests should be added whenever a bug shows that an invariant can be violated silently.

## 1. Layer direction

The dependency direction is:

```text
client_core / ansi_parser / automation / persistence
                    ↓
             session_controller
                    ↓
           Qt bridge / Qt widgets
```

Backend modules and `session_controller.py` must not import PySide6 or modules from `qt/`. Qt may depend on the controller; the controller must not depend on Qt.

## 2. Transport ownership

A `MudSessionController` owns at most one current transport generation. Events from an older generation must never mutate the current session state. Reconnect, disconnect, close, and shutdown must invalidate stale generations before late callbacks can win races.

## 3. Task ownership

Every long-lived asyncio task must have one identifiable owner and a deterministic cancellation/await path. Session-owned background tasks use the UI-neutral `TaskOwner`; Qt window operations use their own window-scoped owner. A task may keep a named reference for policy (for example reconnect), but cancellation/await responsibility remains with one owner. A task must not cancel itself indirectly. Shutdown must be idempotent.

## 4. Subscription lifetime

Every event subscription must have an explicit lifetime and an unsubscribe path. `EventBus.on()` and `MudSessionController.on()` return idempotent `Subscription` handles. Bridges/docks retain and close those handles when their represented session changes or before destruction. One failing subscriber must not prevent independent subscribers or session state processing from continuing.

## 5. Remote-input bounds

Every parser-owned buffer or retained remote-state collection needs an explicit bound appropriate to the protocol. Bounds must be enforced before expensive parsing when practical. Rejecting or discarding malformed remote input must leave the parser capable of processing subsequent valid input.

Current examples include MCCP expansion, Telnet SB frames, ANSI logical lines/control strings, GMCP/MSDP snapshots, protocol event history, and command history.

## 6. Prompt and text ordering

Incoming data flows in this order:

```text
transport → Telnet/protocol decode → ANSI parser → StyledLine/scrollback
          → automation → gag decision → visible Qt output
```

Gagged lines remain available to scrollback/automation. Prompt boundaries (GA/EOR or idle fallback) must not duplicate or lose buffered prompt text.

## 7. Command safety

Manual commands, trigger responses, timers, macros, and future mapper/script commands must ultimately use the typed `CommandPipeline` boundary. Source policy decides whether client commands, aliases, history, and local echo are allowed; producers do not bypass those rules by calling the transport directly. Server-negotiated `WILL ECHO` is authoritative for sensitive input: local echo and local scrollback must not expose password text when the server owns echo. Variable substitution is performed inside this boundary after alias expansion; individual command producers must not implement private substitution rules.

## 8. Persistence atomicity

Persisted configuration is validated as a whole before it replaces live state. Save operations use atomic same-directory replacement. A failed save must leave the previous file intact. Fail-soft application wrappers may recover with defaults, but they must not partially accept a malformed document or silently rewrite the source file.

## 9. Session isolation

No mutable protocol, automation, history, reconnect, bridge, or profile-scoped state may leak between sessions. Failure or shutdown of one session must not change another session.

## 10. Qt destruction ordering

Qt presentation objects do not own transport state. When closing a session tab/window, controller shutdown reaches a safe terminal state before the corresponding bridge/widget is destroyed. Late controller emissions must not target destroyed Qt objects.

## 11. Future extension boundary

Future mapper, scripting, and plugin systems must not receive unrestricted mutable access to Qt/controller internals. Prefer snapshots/read models plus explicit actions/commands through defined APIs.

## 12. Tests are evidence, not a score

A passing test count does not establish correctness by itself. Regression tests must exercise the real production data shape/path that previously failed. New tests are categorized by intent so gaps are visible rather than hidden in one undifferentiated suite.

## Mapper visualization boundary

- Graphical mapper views are projections of repository/controller state.
- The drawing widget must not import the session controller, transport, or command pipeline.
- A graphical room click may select a room; movement remains controller/walker-owned.
- Automatic/fallback layout is presentation state and must not silently overwrite authoritative room coordinates.
