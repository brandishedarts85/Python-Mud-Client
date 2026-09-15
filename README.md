# Python MUD Client — PySide6 desktop client

A standalone Python MUD client with an asyncio Telnet backend and a dockable
PySide6/Qt Widgets desktop shell.

The project keeps protocol, parsing, automation, persistence, and session
orchestration independent of the GUI toolkit.

## Architecture

```text
client_core.py
    asyncio TCP/Telnet transport
    MCCP2 / GMCP / MSDP / NAWS / TTYPE / EOR
        ↓
ansi_parser.py
    ANSI/VT parsing
    StyledLine / Segment / Style
        ↓
automation.py + persistence.py
    aliases / text+style triggers / timers
    profiles / automation / numpad macros / typed variables
        ↓
session_controller.py
    one UI-neutral logical MUD session
        ↓
qt/session_bridge.py
        ↓
qt/
    QMainWindow + session tabs + dock widgets
```


## Project documentation

The repository root is intentionally kept small. Start with:

- `ROADMAP.md` — current feature status and planned work.
- `CHANGELOG.md` — concise release/review history.
- `docs/README.md` — index to architecture contracts, audits, development notes,
  release reports, and external review notes.

Historical engineering reports live under `docs/` instead of competing with
source files at the repository root.

## Current desktop features

- `QMainWindow` with movable, floatable, tab-able `QDockWidget` panels
- persistent window geometry and dock layout via `QSettings`
- multi-session central `QTabWidget`
- ANSI/256-color/truecolor output rendering
- per-session command history
- Connect / Disconnect / Reconnect controls
- editable saved connection profiles
- live GMCP / MSDP / Telnet protocol inspector and event feed
- editable aliases and persistence-safe triggers
- ANSI/color-aware trigger matching
- **Create Trigger from Selection…** on MUD output
- toggleable **physical numpad macro pad** with Base / Ctrl / Alt layers
- numpad assignments persisted in `macros.json`
- global **Settings…** dialog with live reversible appearance preview
- persistent output font, default text/background colors, timestamps, scrollback limit, local echo preference, and new-session reconnect defaults
- per-session plain-text logging with bounded rotation
- `Ctrl+F` transcript search with forward/backward wrap and match-case option
- atomic plain-text transcript export
- profile-aware typed variables with `${name}` substitution
- dockable Variables editor plus `#set`, `#unset`, and `#vars` commands
- profile-scoped SQLite mapper with weighted routing
- generic GMCP `Room.Info` room-identity adapter
- prompt-aware route walking with door/special exits and bounded rerouting
- graphical mapper with pan/zoom, room selection, route highlighting, and coordinate-assisted layout
- root-level `ROADMAP.md` with current and planned feature status


## Logging & Search

Each session can start and stop its own visible-transcript log from the File
menu. Log records are UTF-8 text with ISO timestamps. The logger rotates at
5 MiB and retains at most three previous segments (`.1` through `.3`) so a
long-running session cannot grow one file without bound. Logging is deliberately
presentation-oriented: it records visible MUD output, local command echo, and
client system lines; trigger-gagged input and raw protocol/socket bytes remain
outside the transcript.

A logging I/O failure disables only that session's logger. Incoming MUD text is
still rendered and transport processing continues.

`Ctrl+F` opens a per-session find bar. Search operates on the currently retained
rendered transcript, supports next/previous with wrap-around, and can be made
case-sensitive. **Export Transcript…** atomically writes the current rendered
plain text to a user-selected UTF-8 file. If output timestamps are enabled,
those visible timestamps are naturally included in the exported snapshot.


## Variables

Client variables are typed JSON scalar values (`string`, `int`, `float`, or
`bool`) and use `${name}` substitution in outbound command text. Substitution
happens after alias expansion, so an alias such as `k -> kill ${target}` works
as expected. Manual commands, numpad macros, trigger/timer responses, and the
reserved mapper/script command sources all pass through the same substitution
boundary. Unknown references remain literal instead of silently becoming empty.

Variables are isolated by profile. Unnamed/manual sessions use `variables.json`;
saved profiles use deterministic files under `profiles_data/`. Changes from the
Variables dock or `#set`/`#unset` are atomically persisted immediately, and a
failed save rolls the in-memory value back.

Command examples:

```text
#set target = goblin
#set retries:int = 3
#set threshold:float = 0.75
#set enabled:bool = yes
#vars
#unset target
```

Use `${target}` anywhere in outbound command text.


## Mapper

The mapper stores rooms and directed exits in a profile-scoped SQLite database.
It supports weighted shortest-path routing, stable external room identity, and a
UI-neutral adapter seam for protocol-derived room information. The built-in
generic adapter conservatively understands GMCP `Room.Info` only when a stable
room ID is present.

Automatic walking is acknowledgement-driven rather than a traditional
fire-and-forget speedwalk. Each movement waits for both the expected room
identity and a prompt boundary before the next command may be sent. Door exits
can define a pre-command such as `open north`; that command must receive a
prompt before the actual movement command is issued. Special exits can use
arbitrary movement commands such as `enter portal`.

If a move lands in a different but positively identified mapped room, the
controller may calculate a replacement route to the original destination.
Rerouting is bounded, waits for the current prompt boundary, and stops if the
replacement route would require unidentified rooms.

The Mapper dock also includes a graphical view with drag-to-pan, mouse-wheel
zoom, click-to-select room nodes, current-room highlighting, and active/preview
route highlighting. Explicit room coordinates are honored; rooms without them
receive deterministic fallback positions for display only. **Fit Map** and
**Center Current** help navigate larger maps.

See `ROADMAP.md` for MUD-specific adapters and later mapper organization work.

## Settings & Appearance

Global desktop preferences are stored in `settings.json` using the same v1
persistence envelope and atomic-write rules as the other user state. The
Settings dialog currently controls:

- output font family and size
- default output foreground and background colors
- output timestamps
- bounded scrollback size
- local command echo when the server has not negotiated `WILL ECHO`
- automatic-reconnect defaults for newly created unnamed sessions

Font and color changes preview live because they are reversible. Scrollback,
timestamps, local echo, and reconnect defaults apply only after **OK**, avoiding
lossy or behavior-changing previews while a live session continues to receive
data. Connection profiles remain authoritative for their own reconnect values.

## Numpad macros

The Macro dock mirrors a physical full numpad. Right-click a key to assign a
short label and command. Left-clicking the button or pressing the matching
physical numpad key sends that command through the normal session command path.

Supported layers:

```text
Numpad
Ctrl + Numpad
Alt + Numpad
```

**Enable Numpad Macros** is available in both the dock and Tools menu. When it
is off, no numpad key is intercepted. Even when macros are enabled, an
**unassigned** numpad key is not intercepted, so normal number entry still
works.

## Color-aware triggers

Triggers can optionally require the regex match to use specific parsed ANSI
presentation styles. Foreground/background colors are normalized to RGB, so
the same mechanism works with classic ANSI 16-color, xterm-256, and truecolor.

Example use case:

```text
Text regex: ^You're DYING!!$
Allowed foregrounds: dark red + bright red
Response: quaff healing-potion
```

The same words printed in another color will not fire that trigger.

To capture a real MUD warning:

1. select the text in the MUD output window
2. right-click
3. choose **Create Trigger from Selection…**
4. the trigger dialog is prefilled with the escaped selected text and captured
   foreground/background color
5. optionally use **Add ANSI dark/bright pair** to accept both classic variants
6. enter the response and save the automation

Color matching is performed against `StyledLine` segments, not raw ANSI escape
sequences.

Always follow the automation rules of the MUD you are connected to. A client
feature being available does not imply that a particular server permits its
use.

## Profiles

Profiles can store:

- host / port
- Telnet terminal type
- automatic reconnect enabled/disabled
- reconnect base delay
- reconnect maximum delay

Older host/port-only `profiles.json` entries remain supported.

## Persistence format and migration

Profiles, automation, numpad macros, typed variables, and global client settings now share one versioned persistence
envelope. New writes use schema version 1; historical unversioned files are
read as legacy version 0. Merely opening a legacy file does not rewrite it. On
the first successful save, the client preserves the original bytes as a
`.pre-v1.bak` file and then atomically writes the versioned document.

A file created by a newer persistence schema is rejected rather than guessed
at or overwritten by an older client. See `docs/architecture/PERSISTENCE_SCHEMA.md` for the
format and migration contract.

## Requirements

Python 3.11+ is recommended.

```bash
python -m pip install -r requirements.txt
```

For tests:

```bash
python -m pip install -r requirements-dev.txt
```

## Run

```bash
python app.py
```

Or connect immediately:

```bash
python app.py mud.example.com 4000
```

Or use a saved profile:

```bash
python app.py my-profile
```

## Outbound command and lifecycle boundaries

All command producers now enter the session through a typed command pipeline. Manual input and numpad macros retain normal client-command, alias, history, and local-echo behavior. Trigger/timer output uses a non-recursive automation policy, so it does not re-run aliases or enter command history. Future mapper/script producers already have reserved safe source policies rather than calling the socket directly.

Long-lived session tasks and Qt window operations also have explicit owners, and event subscriptions return closeable handles. This keeps reconnect/shutdown and session-switch teardown deterministic as the client grows.

## Client-side commands

```text
#alias PATTERN = EXPANSION
#unalias PATTERN
#trigger PATTERN = RESPONSE [:: gag oneshot cooldown=N]
#untrigger PATTERN
#list
#set NAME[:TYPE] = VALUE
#unset NAME
#vars
#savevars
#save
#saveprofile NAME
#reconnect
#disconnect
#help
```

The GUI trigger editor is required for style/color constraints; the compact
`#trigger` command continues to create ordinary text-only triggers.

## Tests

```bash
pytest -q
```

Regression coverage includes transport/session behavior, ANSI parsing,
automation, styled trigger matching, persistence, profile compatibility,
protocol state, command history, local-vs-server echo, and numpad macro
persistence.

## Legacy Textual shell

The prior Textual UI remains as `legacy_textual_app.py` for behavioral
reference only. New feature work targets the Qt shell.

### Mixed-color trigger selections

When **Create Trigger from Selection…** is used on a line containing more than
one ANSI color, the editor now captures all foreground/background colors present
in the selected visible text. The style matcher still checks every
non-whitespace character in the regex match, so a one-color warning remains
strictly color-specific while legitimate mixed-color prompts can also match.

### ANSI-aware trigger capture

Output selections retain the parser's original ANSI style metadata instead of
inferring ANSI state from Qt's painted theme colors. Trigger filters can match
explicit RGB colors and the terminal's default/unset foreground/background,
which makes mixed prompts such as default text with a colored name reliable.

### Automation echo

Commands sent by triggers/timers are shown in the transcript when the server is not handling Telnet ECHO. They remain hidden when the server has negotiated `WILL ECHO`, matching the safety behavior of manually submitted commands.

## Automation 2.0 reliability controls

The Automation dock now includes the first reliability/safety pass intended for
daily use:

- **Master Enable automation switch** — disables aliases, triggers, and timers
  without deleting their definitions. Use **Save** to persist the master state.
- **Per-profile automation** — a saved connection profile gets its own alias /
  trigger file under `profiles_data/`. Manual or unnamed sessions continue to
  use the legacy global `automation.json` file.
- **Per-profile numpad macros** — saved profiles also get their own macro file
  under `profiles_data/`; manual sessions continue to use `macros.json`.
- **Trigger priority** — higher numeric priority is evaluated first. Triggers
  with the same priority keep insertion order.
- **Cooldown and one-shot controls** — both are editable in the trigger dialog;
  one-shot state updates in the UI immediately after it fires.
- **Test / Explain** — select a trigger and click **Test**. The dialog evaluates
  without firing and reports master state, trigger state, regex result,
  ANSI/style result, cooldown readiness, and the reason it would or would not
  fire. The latest received `StyledLine` can be reused so color-aware triggers
  are tested against real parsed ANSI metadata.
- **Firing Log** — a bounded per-session log records the time, trigger pattern,
  exact matched text, and expanded response for the latest 200 trigger firings.

Profile-scoped filenames use a readable slug plus a short hash so punctuation,
spaces, and similarly-named profiles cannot collide accidentally. User data in
`profiles_data/` is ignored by Git.

### Current hardening status

Hardening 4 adds crash-resistant atomic JSON replacement (including file flush and
POSIX directory-sync where supported) plus option-specific Telnet subnegotiation
ceilings for TTYPE, MCCP2, GMCP, and MSDP.  See `docs/development/HARDENING_NOTES.md` for the
adversarial cases and design rationale.

## Mapper Foundation

The 1.0 mapper foundation stores rooms and directed exits in SQLite. Maps are profile-scoped, support weighted shortest-path routing, and can be edited from the dockable Mapper panel. Movement initiated by the mapper uses the shared outbound command pipeline. Later mapper releases added stable room identity, adapter-driven discovery, acknowledgement-driven walking, doors/special exits, bounded rerouting, and a graphical map view.

### Mapper 2 — room identity and adapters

The mapper now has a UI-neutral adapter seam. A conservative generic GMCP `Room.Info` adapter can normalize stable room IDs, names, areas, coordinates, and resolvable exits into the profile-scoped SQLite map. The Mapper dock can switch between `Manual only` and the generic adapter. MUD-specific parsing remains outside the core.


### Prompt-aware mapper walking
Mapper routes can be walked one command at a time when a room-identity adapter is active. Each step requires both the expected stable room observation and a prompt boundary before the next command is sent; divergence, timeout, disconnect, or explicit cancellation stops the walk.


## MUD Adapter Framework

Mapper protocol interpretation is extensible through a validated, UI-neutral adapter registry. Built-in choices currently include manual mapping, conservative generic GMCP `Room.Info`, and an evidence-backed `MG.room.info` adapter for LP-MUD/Morgengrauen-style protocol data. Adapters declare capabilities and can only return normalized room observations; they do not receive controller, Qt, transport, or database access.
