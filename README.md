# GMud32-style MUD client — core skeleton

Three independent, UI-agnostic layers, matching the architecture you laid out.
All original code, written from protocol specs (Telnet/RFC 854, MCCP2, GMCP,
MSDP) — nothing derived from GMud32's source or binary.

## Files

- **`client_core.py`** — the engine. Async telnet socket, full IAC
  negotiation state machine, MCCP2 decompression (including correct
  mid-stream activation — see below), GMCP + MSDP subnegotiation parsing,
  NAWS, TTYPE, and a small `EventBus` for pub/sub dispatch.
- **`ansi_parser.py`** — the renderer. Stateful ANSI/VT100 → `StyledLine`
  parser: 16-color, xterm 256-color, and truecolor SGR codes; bold/italic/
  underline/blink/reverse/strike; Unicode-safe; handles escape sequences
  and CRLF splits across chunk boundaries. Includes a bounded `Scrollback`
  buffer with substring search.
- **`automation.py`** — the brain. `Trigger` (regex + gag/one-shot/cooldown),
  `Alias` (`*`-capture expansion), `Timer`, and an `AutomationEngine` that
  ties them to the event bus. Deliberately plain-Python instead of a
  bespoke DSL — a trigger's "action" is just a callable.
- **`example_wire.py`** — ~50 lines showing how a real UI would connect
  the three layers. Not a UI itself.
- **`app.py`** — the minimal usable UI: a [Textual](https://textual.textualize.io/)
  TUI wiring all three layers together. Scrollback pane (with real
  ANSI colors rendered via Rich), a status bar (shows connection state
  and live GMCP vitals when the server sends `Char.Vitals`), an input
  bar with command history, and a 100ms tick loop driving automation
  timers. Run it with:

      pip install -r requirements.txt
      python3 app.py mud.example.com 4000

  Textual was picked over Qt/PySide6 for this first pass specifically
  because `ansi_parser.py`'s `StyledLine`/`Segment` model maps almost
  directly onto Rich's `Text`/`Style` objects — no widget-toolkit color
  translation layer needed yet. A GUI toolkit is still the likely
  long-term choice once split panes, mouse selection, or an embedded
  map view matter; the rendering layer doesn't care which UI consumes
  it.
- **`persistence.py`** — connection profiles and automation (aliases/
  simple triggers) as flat JSON files (`profiles.json`,
  `automation.json`), so a session survives a restart. Run with a
  saved profile name instead of retyping host/port:

      python3 app.py despair

  In-app slash commands (typed into the input bar, never sent to the
  MUD): `#alias P = E`, `#unalias P`, `#trigger P = R [:: gag]
  [oneshot] [cooldown=N]`, `#untrigger P`, `#list`, `#save`,
  `#saveprofile NAME`, `#reconnect`, `#disconnect`, `#help`. Only
  "simple" aliases/triggers (a plain response string, not an arbitrary
  Python callable) round-trip through the JSON files — code-defined
  automation from `example_wire.py`-style scripts stays in code, by
  design.
- **Connection hardening**, also in `app.py`: an unexpected disconnect
  triggers automatic reconnection with exponential backoff (3s, 6s,
  12s, ... capped at 60s; resets to 3s after a successful reconnect).
  `#disconnect` suspends that until `#reconnect` or a fresh connect.
  Servers that mark prompts with *neither* GA nor IAC EOR (some old or
  minimal codebases just don't bother) are handled by a 300ms-idle
  fallback: if the stream goes quiet with an unterminated line still
  buffered, it renders anyway rather than waiting indefinitely for a
  newline that was never coming.

## What's been verified (not just written — actually run)

- Full telnet negotiation round trip against a fake in-process server
  (asyncio `start_server`), including option negotiation, GMCP and MSDP
  subnegotiation parsing.
- **MCCP2's trickiest edge case**: compression is signaled by an
  `IAC SB 86 IAC SE` marker that can arrive *in the middle of a TCP
  chunk*, with compressed bytes immediately following it in the same
  read. A naive "decompress the whole chunk if MCCP is on" approach
  silently corrupts data here — this implementation detects the
  activation byte-by-byte and re-routes the remainder of that same
  chunk (plus everything after) through zlib. Verified with a marker +
  payload crammed into one `write()`, and again with a compressed
  payload split across two separate socket writes to confirm
  decompressor state survives read boundaries.
- ANSI parsing: 16-color, 256-color, and truecolor SGR sequences;
  escape sequences split across two `feed()` calls; `\r\n` split across
  two `feed()` calls (a naive implementation double-counts this as a
  blank line); bare `\r`-only line endings; Unicode/emoji passthrough.
- Automation: alias expansion with capture groups, trigger firing with
  gag suppression, one-shot auto-disable, and timer scheduling/firing.
- **`app.py`**, headless, via Textual's `run_test()` harness against the
  same kind of fake in-process server: connects and renders incoming
  text into scrollback, GMCP `Char.Vitals` updates the status bar,
  an alias registered at runtime expands and sends correctly, and
  up/down arrow command history recall works.
- **Bugs found by testing against real MUDs** (Realms of Despair,
  SMAUG-based, and Aardwolf) rather than by reasoning about the spec:
  the Telnet EOR command byte was wrong (239, not 21, so servers using
  EOR instead of GA to mark prompts were never detected); GA/EOR
  handling emitted the prompt's content as a mislabeled `TEXT` event
  and then a redundant, empty `PROMPT` event; and `ansi_parser.py`
  never advanced its internal buffer pointer after flushing plain text
  with no trailing newline, so a no-newline prompt got re-processed
  and duplicated on the next `feed()` call. All three are exactly the
  kind of bug a synthetic test server won't surface — worth remembering
  next time something seems clean in tests but real servers still find
  a way to break it.
- **`persistence.py`**: round-trips aliases and simple triggers through
  JSON (including gag/one-shot/cooldown fidelity), connection-profile
  save/resolve/override precedence, missing-file handling, and the
  full slash-command flow (`#alias`, `#trigger`, `#list`, `#save`,
  `#saveprofile`, removal) driven through Textual's `run_test()` pilot
  end-to-end.
- **Reconnect/idle-timeout hardening**: an unexpected drop reconnects
  automatically and re-renders correctly with no duplicated output;
  `#disconnect` genuinely suspends auto-reconnect and `#reconnect`
  restores it; a GA/EOR-less prompt renders after the idle window
  without fragmenting normal fast multi-chunk text that happens to
  arrive in quick succession. Building this surfaced one real bug
  worth remembering: `_wire_bus()` was being called again on every
  reconnect, silently appending duplicate handlers to the same
  long-lived `EventBus` and causing every event to fire (and render)
  once per accumulated handler after the first reconnect. Fixed by
  wiring the bus exactly once, in `__init__`, since its handlers read
  `self.conn`/`self.engine` live rather than capturing them at wiring
  time — so a single wiring correctly follows every future reconnect.

Run `python3 -m py_compile *.py` to sanity check, or point
`example_wire.py` at a real MUD to see it end to end.

## Where this differs from what a from-scratch pass usually gets wrong

1. **MCCP2 timing.** Most first attempts activate decompression on the
   `IAC WILL 86` telnet negotiation instead of the actual `IAC SB 86
   IAC SE` marker. That's *not* per spec — the WILL/DO exchange only
   negotiates that MCCP2 is available; the subnegotiation is the actual
   "start now" signal, and it can land anywhere in the byte stream,
   including with compressed bytes trailing it in the same read.
2. **Split reads.** TCP gives you bytes, not messages. Escape sequences,
   CRLF pairs, and UTF-8 multi-byte characters can all be torn across
   two `read()` calls. Both `client_core.py` and `ansi_parser.py` carry
   state across calls specifically to survive this instead of assuming
   a "chunk" lines up with anything meaningful.
3. **IAC escaping.** A literal `0xFF` byte in-band (e.g. in a truecolor
   value or binary MSDP payload) has to be doubled (`IAC IAC`) on send
   and un-doubled on receive, or it gets misread as the start of a
   telnet command.

## Extending it

- Plugins hang off `AutomationEngine.add_event_hook()` for "run on every
  line" behavior, or `add_trigger`/`add_alias`/`add_timer` for the
  common cases — no plugin-specific API needed yet since Python
  callables already are the plugin API.
- A UI layer subscribes to `EventBus` (`TEXT`, `PROMPT`, `GMCP`, `MSDP`,
  `OPTION_CHANGE`, `CONNECTED`/`DISCONNECTED`) and feeds `TEXT`/`PROMPT`
  payloads into `AnsiParser.feed()` to get `StyledLine`s to draw.
- Mapping/sound/split-panes/themes/tabs are all UI or automation-layer
  concerns and don't touch `client_core.py` at all — which is the point
  of the layering.
