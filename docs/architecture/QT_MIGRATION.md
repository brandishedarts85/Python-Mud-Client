# Qt migration notes

## Decision

The primary UI has moved from Textual to **PySide6 + Qt Widgets**.

The backend was not rewritten for Qt. The migration adds a UI-neutral session
orchestration layer and then a thin QObject bridge.

## Added

```text
session_controller.py
qt/
├─ app.py
├─ main_window.py
├─ session_bridge.py
├─ session_widget.py
├─ output_view.py
├─ command_input.py
├─ docks/
│  ├─ connections.py
│  ├─ automation.py
│  ├─ macros.py
│  └─ protocol.py
└─ dialogs/
   ├─ connect.py
   ├─ profiles.py
   └─ settings.py
```

## Kept unchanged in role

```text
client_core.py   transport/protocol
ansi_parser.py   ANSI presentation model

automation.py    aliases/triggers/timers
persistence.py   JSON persistence
```

## Async integration

`MudConnection` is asyncio-native, so the Qt shell uses `qasync` to run asyncio
on the Qt event loop. This avoids introducing a networking thread solely for
GUI integration.

## Intentionally deferred

The first migration pass does not pretend these are complete:

- full automation editor
- macro assignment persistence
- settings editor
- mapper
- logging UI
- variables UI
- plugin management

The docks exist only where useful to prove the workspace architecture and active
session routing.

## Workspace persistence

`QMainWindow.saveGeometry()` / `restoreGeometry()` and `saveState()` /
`restoreState()` are stored with `QSettings`.

Dock object names are stable, which is required for Qt to restore their layout.

## Multi-session direction

The shell already supports multiple central tabs. Each tab owns its own
`MudSessionController`, parser, scrollback, automation engine, command history,
and protocol state. Docks follow the active tab.

A formal `SessionManager` is intentionally deferred until coordination logic
outgrows the small list currently owned by `MainWindow`.

## Startup ordering fix (Python 3.14 + qasync)

The initial `MudSessionController.start()` is deliberately deferred with
`QEventLoop.call_soon(...)` until qasync is actively running. Constructing the
`MainWindow` is safe before `run_forever()`, but `asyncio.create_task()` is not:
Python 3.14 raises `RuntimeError: no running event loop` in that case.


## Telnet terminal type

The Qt client advertises `xterm-256color` for Telnet TTYPE negotiation.
The application name is intentionally not used as a terminal capability string.
`MudSessionController` accepts a `terminal_type` argument so this can later be
exposed as a per-profile or application setting without coupling it to Qt.

## Local command echo

Qt sessions now follow Telnet ECHO negotiation for submitted commands. If the
remote side has not negotiated `WILL ECHO`, the client displays the submitted
command locally and keeps it in session scrollback. If remote `WILL ECHO` is
active (as commonly used for password/no-echo input), local command echo is
suppressed so sensitive input is not painted into the transcript.

Local command echo is presentation history only: it is not reprocessed as
incoming MUD text and therefore does not fire incoming-text triggers.

## Profile and automation management pass

Connection profiles now persist more than host/port while remaining backward compatible with older profile JSON files. Each profile may store:

- host and port
- Telnet terminal type (default `xterm-256color`)
- automatic reconnect enabled/disabled
- reconnect base delay
- reconnect maximum delay

The Qt Connect dialog exposes the same settings per session. The Profiles dialog can create, edit, delete, save, and apply profiles to the active session. Launching with a saved profile name also applies that profile's terminal/reconnect settings.

The Automation dock is now editable for the persistence-safe automation types supported by the backend:

- aliases
- simple response-template triggers

Entries can be added, edited, enabled/disabled, deleted, and explicitly saved to `automation.json`. Runtime Python-callable automation and timers remain outside this editor because the persistence layer deliberately does not serialize arbitrary callables.

## Numpad macro + styled-trigger pass

The old F1–F12 placeholder Macro dock has been replaced by a physical-numpad
layout tailored for full laptop/desktop keypads. Base, Ctrl, and Alt layers are
supported. Bindings contain a label, command, and enabled flag and are stored
in `macros.json`.

A master **Enable Numpad Macros** toggle is persisted. Physical keypad events
are identified using Qt's `KeypadModifier`, so the main-row number keys do not
fire numpad macros. Unassigned keys are deliberately not consumed and continue
to behave as normal numeric input. Shift is not used as a macro layer because
NumLock/Shift keypad behavior varies across Windows keyboards.

Automation can now evaluate parsed ANSI style in addition to plain text.
`TriggerStyleFilter` supports foreground/background RGB lists and optional
attribute constraints. `AutomationEngine.on_styled_text()` is now the normal
session-controller path; legacy `on_text()` remains available but intentionally
will not fire a style-constrained trigger because it has no style evidence.

The output widget exposes **Create Trigger from Selection…**. The trigger editor
prefills the selected text as an escaped regex and captures the rendered color
from the selection. Classic ANSI dark/bright pairs can be added together with
one button. Style constraints serialize safely into `automation.json`; old
text-only trigger files remain backward compatible.

### Windows numpad key detection
Dedicated numpad digits/operators now prefer Windows native VK_NUMPAD/VK_* codes when available, with Qt KeypadModifier handling retained as a cross-platform fallback. This avoids dedicated Num7/Num8/etc. falling through as ordinary text on some Windows/PySide6 keyboard drivers.

### Mixed-color trigger capture fix

Output capture now records all unique presentation styles used by visible
characters in the selection rather than sampling only the first character.
This allows prompts containing multiple ANSI colors (for example white prompt
text with a yellow account name) to produce a satisfiable style-aware trigger.
Uniform-color safety triggers remain strict because their selections still
capture only that one foreground color.

## Shutdown lifecycle hardening

The Qt/qasync shutdown path is explicit. Closing the main window first awaits every
session controller shutdown, including reconnect and tick task cancellation, then
allows Qt to close the final window. `lastWindowClosed` and `aboutToQuit` both stop
the qasync loop, and remaining asyncio tasks are cancelled before the loop closes.
On Windows, a lightweight Qt timer keeps Python signal processing responsive and
Ctrl+C in the launching console requests the same clean window shutdown path.

## Trigger capture: original ANSI metadata

Qt's painted foreground/background colors are no longer treated as the MUD's
ANSI state when creating a trigger from output. `MudOutputView` preserves the
original parsed presentation `Style` as `QTextCharFormat` user metadata and the
capture path reads that metadata back. This prevents terminal palette colors
(such as the UI's black background) from being mistaken for explicit ANSI
colors.

Style filters can also explicitly allow the terminal's default/unset
foreground or background. This is required for prompts where only one word is
colored and the surrounding text is emitted with no explicit SGR foreground.

## Automation command echo

Trigger/timer-generated commands now use the same Telnet echo policy as typed commands. When the server has not negotiated `WILL ECHO`, generated commands are locally rendered into session scrollback/transcript so users can see what automation sent. When remote echo is active, local echo remains suppressed to avoid duplicates and preserve password safety. Automation-generated commands are not added to command history and are not re-run through alias expansion.

## Automation 2.0 reliability pass

Automation remains UI-independent, but `AutomationEngine` now exposes a small
set of reliability primitives consumed by the Qt editor:

- a master automation enable switch covering aliases/triggers/timers
- integer trigger priority (higher values run first)
- non-firing trigger explanation/testing
- trigger-fire observation events

`MudSessionController` owns a bounded 200-entry trigger firing log and remembers
the most recently parsed `StyledLine` so the Qt Test dialog can explain both
text and ANSI/style matching without reconstructing color information from the
widget.

Saved profiles now select profile-scoped automation and numpad-macro files under
`profiles_data/`. The original `automation.json` and `macros.json` remain the
Global/manual-session files, preserving existing installations. Selecting a
saved profile switches the active session to that profile's automation set;
command-line launch with a saved profile name does the same.

The Automation dock shows the active scope, master switch, trigger priority,
one-shot/cooldown state, a non-firing Test dialog, and a Firing Log containing
the exact matched text and expanded response. These controls are deliberately
observational/user-controlled; they do not add any new autonomous behavior on
servers where automation is not permitted.

## Trigger editor laptop-height fix

The trigger editor is now screen-bounded and places its tall configuration form inside a scroll area. The OK/Cancel button footer remains outside the scroll area so it is always reachable on shorter laptop displays.

## Foundation hardening pass

Before further 1.0 feature work, two foundation issues were promoted to
release-blocking fixes:

- MCCP2 decompression is now bounded inside zlib by
  `MAX_MCCP_OUTPUT_PER_READ`; oversized expansion raises a protocol error.
- malformed persisted automation can no longer abort session construction;
  the client starts with an empty automation engine, leaves the source file
  untouched, and reports the validation problem to the user.

See `../development/HARDENING_NOTES.md` for the implementation rationale and regression cases.
