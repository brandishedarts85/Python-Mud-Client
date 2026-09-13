"""
client_core.py -- the engine.

Async telnet transport for a modern MUD client. Handles:
  - raw TCP / TLS connection
  - telnet IAC negotiation (ECHO, SGA, TTYPE, NAWS, EOR, MCCP2, GMCP, MSDP)
  - MCCP2 stream decompression
  - GMCP / MSDP subnegotiation parsing into Python data structures
  - a small pub/sub event dispatcher that the rendering and automation
    layers subscribe to

Nothing here knows about ANSI, triggers, aliases, or any UI toolkit.
This file's only job is: bytes in from the wire -> events out.

Original implementation written from the telnet (RFC 854), MCCP2, GMCP,
and MSDP protocol specifications -- no code derived from any existing
MUD client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import zlib
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Optional

logger = logging.getLogger("mudclient.core")

# --------------------------------------------------------------------------
# Telnet protocol constants
# --------------------------------------------------------------------------

IAC = 255   # Interpret As Command
DONT = 254
DO = 253
WONT = 252
WILL = 251
SB = 250    # Subnegotiation begin
GA = 249    # Go ahead
EL = 248
EC = 247
SE = 240    # Subnegotiation end

# Telnet options this client cares about
OPT_ECHO = 1
OPT_SGA = 3          # Suppress Go Ahead
OPT_TTYPE = 24       # Terminal type
OPT_EOR = 25         # End of record (the *option*, negotiated via WILL/DO)
EOR_MARK = 239       # IAC EOR -- the actual command byte servers send to
                     # mark "this is a complete prompt, no newline follows".
                     # Distinct from OPT_EOR above; easy to conflate.
OPT_NAWS = 31        # Negotiate About Window Size
OPT_MSDP = 69
OPT_MCCP2 = 86
OPT_MCCP3 = 87
OPT_GMCP = 201

_CMD_NAMES = {WILL: "WILL", WONT: "WONT", DO: "DO", DONT: "DONT"}

# MSDP subnegotiation bytes (per the MSDP spec)
MSDP_VAR = 1
MSDP_VAL = 2
MSDP_TABLE_OPEN = 3
MSDP_TABLE_CLOSE = 4
MSDP_ARRAY_OPEN = 5
MSDP_ARRAY_CLOSE = 6


class EventType(Enum):
    CONNECTED = auto()
    DISCONNECTED = auto()
    TEXT = auto()            # a chunk of plain/ANSI game text
    GMCP = auto()            # (package: str, data: Any)
    MSDP = auto()            # (variable: str, value: Any)
    PROMPT = auto()          # text that ended in an EOR/GA marker
    OPTION_CHANGE = auto()   # (option: int, state: str)  state in {will,wont,do,dont}
    ERROR = auto()


@dataclass
class Event:
    type: EventType
    data: Any = None


class EventBus:
    """Minimal synchronous pub/sub. Handlers should not block for long;
    if they need to await something, schedule it with asyncio.create_task."""

    def __init__(self) -> None:
        self._subs: dict[EventType, list[Callable[[Event], None]]] = {}

    def on(self, event_type: EventType, handler: Callable[[Event], None]) -> None:
        self._subs.setdefault(event_type, []).append(handler)

    def off(self, event_type: EventType, handler: Callable[[Event], None]) -> None:
        if event_type in self._subs and handler in self._subs[event_type]:
            self._subs[event_type].remove(handler)

    def emit(self, event_type: EventType, data: Any = None) -> None:
        for handler in self._subs.get(event_type, ()):
            try:
                handler(Event(event_type, data))
            except Exception:  # a broken subscriber should never kill the client
                logger.exception("event handler raised for %s", event_type)


class _TelnetState(Enum):
    DATA = auto()
    IAC_SEEN = auto()
    NEGOTIATING = auto()   # after WILL/WONT/DO/DONT, awaiting option byte
    SUBNEG = auto()        # inside SB ... SE, collecting bytes
    SUBNEG_IAC = auto()    # inside SUBNEG, saw an IAC (watching for IAC SE)


class MudConnection:
    """
    Owns one telnet/TLS connection to a MUD.

    Usage:
        conn = MudConnection(host, port, event_bus, use_tls=False)
        await conn.connect()
        conn.send_line("look")
        ...
        await conn.disconnect()
    """

    def __init__(
        self,
        host: str,
        port: int,
        bus: EventBus,
        use_tls: bool = False,
        terminal_type: str = "MudClient",
        encoding: str = "utf-8",
    ) -> None:
        self.host = host
        self.port = port
        self.bus = bus
        self.use_tls = use_tls
        self.terminal_type = terminal_type
        self.encoding = encoding

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._read_task: Optional[asyncio.Task] = None

        # telnet option negotiation state, keyed by option byte
        self.options_enabled: set[int] = set()

        # parser state
        self._state = _TelnetState.DATA
        self._sb_option: Optional[int] = None
        self._sb_buffer = bytearray()
        self._pending_cmd: Optional[int] = None  # WILL/WONT/DO/DONT awaiting option byte

        # MCCP2: once the server confirms it, all *subsequent* bytes on the
        # socket are zlib-compressed until the stream ends.
        self._mccp_active = False
        self._decompressor: Optional[zlib.decompressobj] = None
        self._raw_queue = bytearray()  # bytes read from the socket, not yet parsed
        self._mccp_just_activated = False  # one-shot: set for the byte where MCCP2 turns on

        # partial UTF-8 bytes that didn't decode cleanly yet (split across reads)
        self._pending_text_bytes = bytearray()

        # current terminal size, sent via NAWS
        self.cols = 80
        self.rows = 24

    # -- connection lifecycle -----------------------------------------

    async def connect(self) -> None:
        ssl_ctx = ssl.create_default_context() if self.use_tls else None
        self._reader, self._writer = await asyncio.open_connection(
            self.host, self.port, ssl=ssl_ctx
        )
        self.bus.emit(EventType.CONNECTED, {"host": self.host, "port": self.port})
        self._read_task = asyncio.create_task(self._read_loop())

    async def disconnect(self) -> None:
        if self._read_task:
            self._read_task.cancel()
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
        self.bus.emit(EventType.DISCONNECTED, None)

    async def wait_closed(self) -> None:
        if self._read_task:
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass

    # -- outgoing ---------------------------------------------------------

    def send_line(self, line: str) -> None:
        self._raw_send((line + "\r\n").encode(self.encoding, errors="replace"))

    def send_raw(self, data: bytes) -> None:
        self._raw_send(data)

    def _raw_send(self, data: bytes) -> None:
        # escape any literal 0xFF bytes so they aren't mistaken for IAC
        data = data.replace(bytes([IAC]), bytes([IAC, IAC]))
        if self._writer is None:
            raise RuntimeError("not connected")
        self._writer.write(data)

    def send_naws(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows
        if OPT_NAWS not in self.options_enabled:
            return
        payload = cols.to_bytes(2, "big") + rows.to_bytes(2, "big")
        self._writer.write(bytes([IAC, SB, OPT_NAWS]) + payload + bytes([IAC, SE]))

    def send_gmcp(self, package: str, data: Any = None) -> None:
        body = package if data is None else f"{package} {json.dumps(data)}"
        payload = bytes([IAC, SB, OPT_GMCP]) + body.encode("utf-8") + bytes([IAC, SE])
        self._writer.write(payload)

    def send_msdp(self, variable: str, value: str) -> None:
        payload = (
            bytes([IAC, SB, OPT_MSDP, MSDP_VAR])
            + variable.encode("ascii")
            + bytes([MSDP_VAL])
            + value.encode("ascii")
            + bytes([IAC, SE])
        )
        self._writer.write(payload)

    # -- reading / parsing --------------------------------------------

    async def _read_loop(self) -> None:
        try:
            while True:
                chunk = await self._reader.read(4096)
                if not chunk:
                    break
                self._raw_queue.extend(chunk)
                self._drain_queue()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("read loop error")
            self.bus.emit(EventType.ERROR, str(exc))
        finally:
            self.bus.emit(EventType.DISCONNECTED, None)

    def _drain_queue(self) -> None:
        """Pulls bytes off self._raw_queue and feeds them to the telnet
        parser. MCCP2 can start mid-stream (the moment the IAC SB 86 IAC
        SE marker is parsed), so this alternates between feeding raw
        bytes and decompressed bytes rather than deciding once per
        socket read -- everything *after* the marker, including bytes
        already sitting in this same chunk, must go through zlib first."""
        while self._raw_queue:
            if self._mccp_active:
                try:
                    decompressed = self._decompressor.decompress(bytes(self._raw_queue))
                except zlib.error:
                    logger.warning("MCCP2 stream error; disabling decompression")
                    self._mccp_active = False
                    continue  # retry this same data as raw
                self._raw_queue.clear()
                if decompressed:
                    self._feed(decompressed)
                return
            else:
                consumed = self._feed(bytes(self._raw_queue))
                del self._raw_queue[:consumed]
                if not self._mccp_active:
                    return  # fully consumed, nothing pending to reinterpret

    def _feed(self, chunk: bytes) -> int:
        """Telnet IAC state machine. Splits the byte stream into plain
        text runs and out-of-band command/subnegotiation blocks.
        Returns the number of bytes consumed from `chunk`; stops early
        (returning less than len(chunk)) the instant MCCP2 activates,
        since every remaining byte must be decompressed before it means
        anything to this parser."""
        text_run = bytearray()

        def flush_text() -> None:
            if text_run:
                self._pending_text_bytes.extend(text_run)
                text_run.clear()
                self._emit_text()

        idx = 0
        n = len(chunk)
        while idx < n:
            b = chunk[idx]
            idx += 1
            st = self._state
            if st == _TelnetState.DATA:
                if b == IAC:
                    self._state = _TelnetState.IAC_SEEN
                else:
                    text_run.append(b)

            elif st == _TelnetState.IAC_SEEN:
                if b == IAC:               # escaped 0xFF literal
                    text_run.append(IAC)
                    self._state = _TelnetState.DATA
                elif b in (WILL, WONT, DO, DONT):
                    self._pending_cmd = b
                    self._state = _TelnetState.NEGOTIATING
                elif b == SB:
                    self._sb_buffer.clear()
                    self._sb_option = None
                    self._state = _TelnetState.SUBNEG
                elif b in (GA, EOR_MARK):  # GA (249) or IAC EOR (239)
                    # NOTE: deliberately NOT flush_text() here -- that
                    # emits a TEXT event and clears the pending buffer,
                    # which would leave _emit_prompt() with nothing to
                    # send. Move the accumulated bytes over directly so
                    # they go out as PROMPT, not TEXT.
                    if text_run:
                        self._pending_text_bytes.extend(text_run)
                        text_run.clear()
                    self._emit_prompt()
                    self._state = _TelnetState.DATA
                else:
                    # NOP or other single-byte command; ignore
                    self._state = _TelnetState.DATA

            elif st == _TelnetState.NEGOTIATING:
                self._handle_negotiation(self._pending_cmd, b)
                self._pending_cmd = None
                self._state = _TelnetState.DATA

            elif st == _TelnetState.SUBNEG:
                if self._sb_option is None:
                    self._sb_option = b
                elif b == IAC:
                    self._state = _TelnetState.SUBNEG_IAC
                else:
                    self._sb_buffer.append(b)

            elif st == _TelnetState.SUBNEG_IAC:
                if b == SE:
                    self._handle_subnegotiation(self._sb_option, bytes(self._sb_buffer))
                    self._sb_buffer.clear()
                    self._sb_option = None
                    self._state = _TelnetState.DATA
                elif b == IAC:
                    self._sb_buffer.append(IAC)
                    self._state = _TelnetState.SUBNEG
                else:
                    # malformed; bail back to subneg-collecting
                    self._sb_buffer.append(b)
                    self._state = _TelnetState.SUBNEG

            if self._mccp_just_activated:
                # compression just activated (inside _handle_subnegotiation,
                # triggered by the byte we just consumed): everything from
                # here on in this chunk is compressed and must go back
                # through _drain_queue's decompression path.
                self._mccp_just_activated = False
                flush_text()
                return idx

        flush_text()
        return n

    def _emit_text(self) -> None:
        # decode as much of the pending buffer as is valid UTF-8; keep any
        # trailing partial multi-byte sequence for the next chunk
        data = bytes(self._pending_text_bytes)
        text, consumed = _decode_utf8_prefix(data)
        if text:
            self.bus.emit(EventType.TEXT, text)
        self._pending_text_bytes = bytearray(data[consumed:])

    def _emit_prompt(self) -> None:
        data = bytes(self._pending_text_bytes)
        text = data.decode("utf-8", errors="replace")
        self._pending_text_bytes.clear()
        self.bus.emit(EventType.PROMPT, text)

    # -- option negotiation --------------------------------------------

    def _handle_negotiation(self, cmd: int, option: int) -> None:
        writer = self._writer
        assert writer is not None
        name = _CMD_NAMES.get(cmd, str(cmd))
        logger.debug("telnet %s %d", name, option)

        if cmd == WILL:
            if option in (OPT_SGA, OPT_ECHO, OPT_EOR, OPT_MCCP2, OPT_GMCP, OPT_MSDP):
                writer.write(bytes([IAC, DO, option]))
                self.options_enabled.add(option)
                # NOTE: MCCP2 does NOT start here. Per spec, the server
                # signals "compression starts now" with an explicit
                # IAC SB 86 IAC SE subnegotiation; everything after that
                # marker's IAC SE is compressed. See _handle_subnegotiation.
                self.bus.emit(EventType.OPTION_CHANGE, {"option": option, "state": "will"})
            else:
                writer.write(bytes([IAC, DONT, option]))

        elif cmd == WONT:
            self.options_enabled.discard(option)
            if option == OPT_MCCP2:
                self._mccp_active = False
                self._decompressor = None
            self.bus.emit(EventType.OPTION_CHANGE, {"option": option, "state": "wont"})

        elif cmd == DO:
            if option == OPT_TTYPE:
                writer.write(bytes([IAC, WILL, OPT_TTYPE]))
            elif option == OPT_NAWS:
                writer.write(bytes([IAC, WILL, OPT_NAWS]))
                self.options_enabled.add(OPT_NAWS)
                self.send_naws(self.cols, self.rows)
            elif option in (OPT_SGA,):
                writer.write(bytes([IAC, WILL, option]))
            else:
                writer.write(bytes([IAC, WONT, option]))

        elif cmd == DONT:
            self.options_enabled.discard(option)
            self.bus.emit(EventType.OPTION_CHANGE, {"option": option, "state": "dont"})

    def _start_mccp(self) -> None:
        self._decompressor = zlib.decompressobj()
        self._mccp_active = True
        self._mccp_just_activated = True
        logger.info("MCCP2 compression active")

    def _handle_subnegotiation(self, option: Optional[int], payload: bytes) -> None:
        if option == OPT_TTYPE:
            # server asked (SEND); reply with our terminal type
            if payload[:1] == b"\x01":
                body = bytes([0]) + self.terminal_type.encode("ascii", errors="replace")
                self._writer.write(bytes([IAC, SB, OPT_TTYPE]) + body + bytes([IAC, SE]))
        elif option == OPT_MCCP2:
            # the empty SB...SE marker itself is the "start compressing now"
            # signal; every byte the server sends after this subnegotiation's
            # IAC SE is zlib-compressed.
            self._start_mccp()
        elif option == OPT_GMCP:
            self._handle_gmcp(payload)
        elif option == OPT_MSDP:
            self._handle_msdp(payload)
        else:
            logger.debug("unhandled subnegotiation for option %s (%d bytes)", option, len(payload))

    def _handle_gmcp(self, payload: bytes) -> None:
        text = payload.decode("utf-8", errors="replace")
        if " " in text:
            package, _, json_part = text.partition(" ")
            try:
                data = json.loads(json_part)
            except json.JSONDecodeError:
                data = json_part
        else:
            package, data = text, None
        self.bus.emit(EventType.GMCP, {"package": package, "data": data})

    def _handle_msdp(self, payload: bytes) -> None:
        try:
            parsed = _parse_msdp(payload)
        except Exception:
            logger.exception("failed to parse MSDP payload")
            return
        for variable, value in parsed.items():
            self.bus.emit(EventType.MSDP, {"variable": variable, "value": value})


def _decode_utf8_prefix(data: bytes) -> tuple[str, int]:
    """Decode the longest valid-UTF8 prefix of data. Returns (text, bytes_consumed).
    Any trailing incomplete multi-byte sequence is left unconsumed so it can
    be completed by the next chunk."""
    if not data:
        return "", 0
    try:
        return data.decode("utf-8"), len(data)
    except UnicodeDecodeError as exc:
        # decode what's good, leave the tail (likely a split multi-byte char)
        good = data[: exc.start]
        return good.decode("utf-8", errors="replace"), exc.start


def _parse_msdp(payload: bytes) -> dict[str, Any]:
    """Parses an MSDP subnegotiation body into a flat/nested dict.
    Supports VAR/VAL pairs and nested TABLE/ARRAY structures."""
    pos = 0

    def parse_value() -> Any:
        nonlocal pos
        if pos < len(payload) and payload[pos] == MSDP_ARRAY_OPEN:
            pos += 1
            items = []
            while pos < len(payload) and payload[pos] != MSDP_ARRAY_CLOSE:
                if payload[pos] == MSDP_VAL:
                    pos += 1
                items.append(parse_value())
            if pos < len(payload) and payload[pos] == MSDP_ARRAY_CLOSE:
                pos += 1
            return items
        if pos < len(payload) and payload[pos] == MSDP_TABLE_OPEN:
            pos += 1
            table: dict[str, Any] = {}
            while pos < len(payload) and payload[pos] != MSDP_TABLE_CLOSE:
                if payload[pos] == MSDP_VAR:
                    pos += 1
                name = read_until([MSDP_VAL])
                if pos < len(payload) and payload[pos] == MSDP_VAL:
                    pos += 1
                table[name] = parse_value()
            if pos < len(payload) and payload[pos] == MSDP_TABLE_CLOSE:
                pos += 1
            return table
        return read_until([MSDP_VAR, MSDP_VAL, MSDP_TABLE_CLOSE, MSDP_ARRAY_CLOSE])

    def read_until(stop_bytes: list[int]) -> str:
        nonlocal pos
        start = pos
        while pos < len(payload) and payload[pos] not in stop_bytes:
            pos += 1
        return payload[start:pos].decode("utf-8", errors="replace")

    result: dict[str, Any] = {}
    while pos < len(payload):
        if payload[pos] == MSDP_VAR:
            pos += 1
            name = read_until([MSDP_VAL])
            if pos < len(payload) and payload[pos] == MSDP_VAL:
                pos += 1
            result[name] = parse_value()
        else:
            pos += 1  # skip stray byte defensively
    return result
