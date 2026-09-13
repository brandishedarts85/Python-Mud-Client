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
