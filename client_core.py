"""
client_core.py -- the transport engine.

Async Telnet transport for a modern MUD client.

Responsibilities:
  - raw TCP / optional TLS connection
  - Telnet IAC parsing and option negotiation
  - ECHO / SGA / TTYPE / NAWS / EOR
  - MCCP2 decompression
  - GMCP / MSDP subnegotiation
  - incremental text framing
  - synchronous event publication

This module deliberately knows nothing about ANSI rendering, aliases,
triggers, automation policy, or any UI toolkit.

Architecture:

    socket bytes
        ↓
    optional MCCP2 decompression
        ↓
    Telnet parser
        ├─ TEXT
        ├─ PROMPT
        ├─ GMCP
        ├─ MSDP
        └─ OPTION_CHANGE
        ↓
    EventBus

Protocol rule:
    recognize defensively, fail closed on corrupted framing, and never
    reinterpret compressed/protocol bytes as ordinary game text.
"""

from __future__ import annotations

import asyncio
import codecs
import json
import logging
import ssl
import time
import zlib

from collections import deque
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Callable, Optional

from lifecycle import Subscription


logger = logging.getLogger("mudclient.core")


# ---------------------------------------------------------------------------
# Telnet protocol constants
# ---------------------------------------------------------------------------

IAC = 255
DONT = 254
DO = 253
WONT = 252
WILL = 251
SB = 250

GA = 249
EL = 248
EC = 247

SE = 240

# End-of-record command byte.
EOR_MARK = 239


# Telnet options.
OPT_ECHO = 1
OPT_SGA = 3
OPT_TTYPE = 24
OPT_EOR = 25
OPT_NAWS = 31
OPT_MSDP = 69
OPT_MCCP2 = 86
OPT_MCCP3 = 87
OPT_GMCP = 201


_CMD_NAMES = {
    WILL: "WILL",
    WONT: "WONT",
    DO: "DO",
    DONT: "DONT",
}


# MSDP framing bytes.
MSDP_VAR = 1
MSDP_VAL = 2
MSDP_TABLE_OPEN = 3
MSDP_TABLE_CLOSE = 4
MSDP_ARRAY_OPEN = 5
MSDP_ARRAY_CLOSE = 6


# Defensive protocol limits.
MAX_SUBNEGOTIATION_BYTES = 1024 * 1024
# Option-specific ceilings let us reject oversized known protocol payloads
# before the generic Telnet SB buffer reaches its much larger fallback limit.
MAX_TTYPE_BYTES = 4 * 1024
MAX_MCCP2_NEGOTIATION_BYTES = 4 * 1024
MAX_GMCP_BYTES = 256 * 1024
MAX_MSDP_BYTES = 256 * 1024
MAX_PENDING_TEXT_BYTES = 1024 * 1024
MAX_MSDP_NESTING = 64
# Maximum decompressed MCCP2 output accepted from one socket read.
# The +1 sentinel used at the call site lets us detect overflow without ever
# allowing zlib to materialize an unbounded expansion in memory.
MAX_MCCP_OUTPUT_PER_READ = 1024 * 1024

# Repeated Telnet negotiation can otherwise become a CPU/write amplifier when
# a broken or hostile peer loops WILL/WONT/DO/DONT. Normal setup uses only a
# handful of transitions per option, so this budget is intentionally generous.
NEGOTIATION_CHURN_WINDOW_SECONDS = 5.0
MAX_NEGOTIATIONS_PER_OPTION_WINDOW = 24
NEGOTIATION_CHURN_SUPPRESS_SECONDS = 10.0


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class EventType(Enum):
    CONNECTED = auto()
    DISCONNECTED = auto()

    # Arbitrary transport text chunk. Not necessarily a semantic line.
    TEXT = auto()

    GMCP = auto()
    MSDP = auto()

    # Text immediately preceding GA / IAC EOR.
    PROMPT = auto()

    OPTION_CHANGE = auto()

    # Diagnostic-only protocol event. It must not masquerade as option state.
    PROTOCOL_NOTICE = auto()

    ERROR = auto()


@dataclass(frozen=True)
class Event:
    type: EventType
    data: Any = None


class EventBus:
    """
    Minimal synchronous pub/sub bus.

    Subscribers should return quickly. Async work should be scheduled by the
    subscriber rather than awaited from emit().
    """

    def __init__(self) -> None:
        self._subs: dict[
            EventType,
            list[Callable[[Event], None]],
        ] = {}

    def on(
        self,
        event_type: EventType,
        handler: Callable[[Event], None],
    ) -> Subscription:
        handlers = self._subs.setdefault(
            event_type,
            [],
        )

        if handler not in handlers:
            handlers.append(handler)

        return Subscription(lambda: self.off(event_type, handler))

    def off(
        self,
        event_type: EventType,
        handler: Callable[[Event], None],
    ) -> None:
        handlers = self._subs.get(
            event_type
        )

        if not handlers:
            return

        try:
            handlers.remove(handler)

        except ValueError:
            return

        if not handlers:
            self._subs.pop(
                event_type,
                None,
            )

    def emit(
        self,
        event_type: EventType,
        data: Any = None,
    ) -> None:
        event = Event(
            event_type,
            data,
        )

        # Snapshot protects iteration when a callback subscribes/unsubscribes
        # while an event is being dispatched.
        for handler in tuple(
            self._subs.get(
                event_type,
                (),
            )
        ):
            try:
                handler(event)

            except Exception:
                # Subscriber failures must never take down the transport.
                logger.exception(
                    "event handler raised for %s",
                    event_type,
                )


# ---------------------------------------------------------------------------
# Internal protocol state
# ---------------------------------------------------------------------------


class _TelnetState(Enum):
    DATA = auto()

    # Saw IAC and are waiting for the command byte.
    IAC_SEEN = auto()

    # Saw WILL/WONT/DO/DONT and are waiting for the option byte.
    NEGOTIATING = auto()

    # Inside IAC SB ... IAC SE.
    SUBNEG = auto()

    # Inside subnegotiation and just saw IAC.
    SUBNEG_IAC = auto()


class TelnetProtocolError(RuntimeError):
    """Raised when Telnet framing becomes unsafe to continue parsing."""


def _subnegotiation_limit(option: Optional[int]) -> int:
    """Return the maximum payload retained for one Telnet SB frame.

    Unknown options use the generic ceiling.  Known protocols use tighter
    bounds so a hostile peer cannot force the client to retain a full generic
    megabyte before the option-specific handler gets a chance to reject it.
    """

    if option == OPT_TTYPE:
        return MAX_TTYPE_BYTES
    if option == OPT_MCCP2:
        return MAX_MCCP2_NEGOTIATION_BYTES
    if option == OPT_GMCP:
        return MAX_GMCP_BYTES
    if option == OPT_MSDP:
        return MAX_MSDP_BYTES
    return MAX_SUBNEGOTIATION_BYTES


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


class MudConnection:
    """
    Own one Telnet/TCP connection to a MUD.

    Typical use:

        bus = EventBus()
        conn = MudConnection("example.org", 4000, bus)

        await conn.connect()

        conn.send_line("look")

        ...

        await conn.disconnect()

    A MudConnection object represents one transport lifecycle. It may be
    explicitly disconnected and reconnected, but application code will usually
    create a new instance for each reconnect generation.
    """

    def __init__(
        self,
        host: str,
        port: int,
        bus: EventBus,
        use_tls: bool = False,
        terminal_type: str = "xterm-256color",
        encoding: str = "utf-8",
    ) -> None:
        self.host = host
        self.port = port
        self.bus = bus

        self.use_tls = use_tls
        self.terminal_type = terminal_type
        self.encoding = encoding

        self._reader: Optional[
            asyncio.StreamReader
        ] = None

        self._writer: Optional[
            asyncio.StreamWriter
        ] = None

        self._read_task: Optional[
            asyncio.Task
        ] = None

        # ------------------------------------------------------------------
        # Negotiation state
        #
        # remote_options:
        #   options the remote/server side WILL perform for us.
        #
        # local_options:
        #   options we WILL perform after the server sends DO.
        #
        # options_enabled remains as a compatibility-facing aggregate used by
        # existing client code.
        # ------------------------------------------------------------------

        self._remote_options: set[int] = set()
        self._local_options: set[int] = set()

        self.options_enabled: set[int] = set()

        # Negotiation-churn accounting is per option. Telnet option bytes are
        # inherently bounded to 0..255, and each deque is pruned to a short
        # monotonic-time window.
        self._negotiation_times: dict[int, deque[float]] = {}
        self._negotiation_suppressed_until: dict[int, float] = {}

        # ------------------------------------------------------------------
        # Telnet parser state
        # ------------------------------------------------------------------

        self._state = _TelnetState.DATA

        self._pending_cmd: Optional[int] = None

        self._sb_option: Optional[int] = None
        self._sb_buffer = bytearray()

        # ------------------------------------------------------------------
        # MCCP
        # ------------------------------------------------------------------

        self._mccp_active = False

        self._decompressor: Optional[
            zlib.decompressobj
        ] = None

        self._raw_queue = bytearray()

        # Set during parsing when the MCCP2 marker's closing IAC SE is
        # consumed. Everything after that exact byte belongs to zlib.
        self._mccp_just_activated = False

        # ------------------------------------------------------------------
        # Text decoding
        #
        # We retain only bytes that form an incomplete trailing multibyte
        # sequence. Invalid complete byte sequences are replaced and consumed,
        # preventing malformed text from wedging the stream indefinitely.
        # ------------------------------------------------------------------

        self._pending_text_bytes = bytearray()

        # Terminal dimensions advertised through NAWS.
        self.cols = 80
        self.rows = 24

        # Lifecycle.
        self._disconnect_emitted = False
        self._disconnecting = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        if self._writer is not None:
            raise RuntimeError(
                "connection is already open"
            )

        self._reset_transport_state()

        ssl_context = (
            ssl.create_default_context()
            if self.use_tls
            else None
        )

        reader, writer = await asyncio.open_connection(
            self.host,
            self.port,
            ssl=ssl_context,
        )

        self._reader = reader
        self._writer = writer

        self.bus.emit(
            EventType.CONNECTED,
            {
                "host": self.host,
                "port": self.port,
            },
        )

        self._read_task = asyncio.create_task(
            self._read_loop(),
            name=f"mud-read:{self.host}:{self.port}",
        )

    async def disconnect(self) -> None:
        """
        Close the transport exactly once.

        DISCONNECTED is emitted at most once regardless of whether shutdown was
        initiated locally, by EOF, by cancellation, or by a read-loop error.
        Cleanup is guaranteed even if the *caller* of disconnect() is itself
        cancelled while waiting for the socket/read task to finish.
        """

        if self._disconnecting:
            return

        self._disconnecting = True

        read_task = self._read_task
        writer = self._writer
        current_task = asyncio.current_task()

        try:
            if (
                read_task is not None
                and not read_task.done()
                and read_task is not current_task
            ):
                read_task.cancel()

            if writer is not None:
                writer.close()

                try:
                    await writer.wait_closed()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Transport shutdown failure should not prevent lifecycle
                    # completion.
                    pass

            if (
                read_task is not None
                and read_task is not current_task
                and not read_task.done()
            ):
                try:
                    await read_task
                except asyncio.CancelledError:
                    # Cancellation of the read task is expected during normal
                    # local shutdown.  If *this* disconnect task was cancelled,
                    # that cancellation has already propagated from an earlier
                    # await and the outer finally still performs cleanup.
                    pass
                except Exception:
                    # _read_loop already reports transport failures.
                    pass
        finally:
            self._reader = None
            self._writer = None
            self._read_task = None
            self._emit_disconnected_once()
            self._disconnecting = False

    async def wait_closed(self) -> None:
        task = self._read_task

        if task is None:
            return

        try:
            await task

        except asyncio.CancelledError:
            pass

    def _reset_transport_state(self) -> None:
        self._state = _TelnetState.DATA
        self._pending_cmd = None

        self._sb_option = None
        self._sb_buffer.clear()

        self._remote_options.clear()
        self._local_options.clear()
        self.options_enabled.clear()
        self._negotiation_times.clear()
        self._negotiation_suppressed_until.clear()

        self._mccp_active = False
        self._decompressor = None
        self._raw_queue.clear()
        self._mccp_just_activated = False

        self._pending_text_bytes.clear()

        self._disconnect_emitted = False
        self._disconnecting = False

    def _emit_disconnected_once(self) -> None:
        if self._disconnect_emitted:
            return

        self._disconnect_emitted = True

        self.bus.emit(
            EventType.DISCONNECTED,
            None,
        )

    # ------------------------------------------------------------------
    # Outgoing application data
    # ------------------------------------------------------------------

    def send_line(
        self,
        line: str,
    ) -> None:
        payload = (
            line
            + "\r\n"
        ).encode(
            self.encoding,
            errors="replace",
        )

        self._raw_send(
            payload
        )

    def send_raw(
        self,
        data: bytes,
    ) -> None:
        """
        Send application data through Telnet DATA mode.

        Literal IAC bytes are doubled as required by Telnet.
        """

        self._raw_send(
            data
        )

    def _raw_send(
        self,
        data: bytes,
    ) -> None:
        writer = self._require_writer()

        writer.write(
            _escape_iac(data)
        )

    # ------------------------------------------------------------------
    # Outgoing Telnet protocol data
    # ------------------------------------------------------------------

    def _send_telnet_command(
        self,
        *values: int,
    ) -> None:
        writer = self._require_writer()

        writer.write(
            bytes(values)
        )

    def _send_subnegotiation(
        self,
        option: int,
        payload: bytes = b"",
    ) -> None:
        """
        Send IAC SB <option> <payload> IAC SE.

        Literal IAC bytes inside the payload are doubled.
        """

        writer = self._require_writer()

        framed = (
            bytes(
                [
                    IAC,
                    SB,
                    option,
                ]
            )
            + _escape_iac(payload)
            + bytes(
                [
                    IAC,
                    SE,
                ]
            )
        )

        writer.write(
            framed
        )

    def send_naws(
        self,
        cols: int,
        rows: int,
    ) -> None:
        if not (
            0 <= cols <= 65535
        ):
            raise ValueError(
                "cols must be between 0 and 65535"
            )

        if not (
            0 <= rows <= 65535
        ):
            raise ValueError(
                "rows must be between 0 and 65535"
            )

        self.cols = cols
        self.rows = rows

        if OPT_NAWS not in self._local_options:
            return

        payload = (
            cols.to_bytes(
                2,
                "big",
            )
            + rows.to_bytes(
                2,
                "big",
            )
        )

        # Important: dimensions such as 255 contain literal 0xFF and therefore
        # MUST pass through Telnet IAC escaping.
        self._send_subnegotiation(
            OPT_NAWS,
            payload,
        )

    def send_gmcp(
        self,
        package: str,
        data: Any = None,
    ) -> None:
        if not package:
            raise ValueError(
                "GMCP package must not be empty"
            )

        if data is None:
            body = package

        else:
            body = (
                f"{package} "
                f"{json.dumps(data, separators=(',', ':'))}"
            )

        payload = body.encode(
            "utf-8",
            errors="strict",
        )

        if len(payload) > MAX_GMCP_BYTES:
            raise ValueError(
                "GMCP payload exceeds maximum size"
            )

        self._send_subnegotiation(
            OPT_GMCP,
            payload,
        )

    def send_msdp(
        self,
        variable: str,
        value: str,
    ) -> None:
        variable_bytes = variable.encode(
            "utf-8",
            errors="strict",
        )

        value_bytes = value.encode(
            "utf-8",
            errors="strict",
        )

        payload = (
            bytes(
                [
                    MSDP_VAR,
                ]
            )
            + variable_bytes
            + bytes(
                [
                    MSDP_VAL,
                ]
            )
            + value_bytes
        )

        if len(payload) > MAX_MSDP_BYTES:
            raise ValueError(
                "MSDP payload exceeds maximum size"
            )

        self._send_subnegotiation(
            OPT_MSDP,
            payload,
        )

    def _require_writer(
        self,
    ) -> asyncio.StreamWriter:
        writer = self._writer

        if writer is None:
            raise RuntimeError(
                "not connected"
            )

        if writer.is_closing():
            raise RuntimeError(
                "connection is closing"
            )

        return writer

    # ------------------------------------------------------------------
    # Reader
    # ------------------------------------------------------------------

    async def _read_loop(self) -> None:
        reader = self._reader

        if reader is None:
            return

        try:
            while True:
                chunk = await reader.read(
                    4096
                )

                if not chunk:
                    break

                self._raw_queue.extend(
                    chunk
                )

                self._drain_queue()

        except asyncio.CancelledError:
            raise

        except TelnetProtocolError as exc:
            logger.warning(
                "telnet protocol error: %s",
                exc,
            )

            self.bus.emit(
                EventType.ERROR,
                str(exc),
            )

        except Exception as exc:
            logger.exception(
                "read loop error"
            )

            self.bus.emit(
                EventType.ERROR,
                str(exc),
            )

        finally:
            # Flush any complete final text. An incomplete trailing codepoint is
            # emitted with replacement because there will be no future bytes to
            # complete it.
            self._emit_final_text()

            writer = self._writer

            if writer is not None:
                writer.close()

            self._emit_disconnected_once()

    # ------------------------------------------------------------------
    # MCCP / raw queue
    # ------------------------------------------------------------------

    def _drain_queue(self) -> None:
        """
        Drain bytes while respecting the exact transition point into MCCP2.

        Compression may begin in the middle of one socket read:

            ... IAC SB MCCP2 IAC SE <compressed bytes>

        Bytes following that SE must never be parsed as ordinary Telnet data.
        """

        while self._raw_queue:
            if not self._mccp_active:
                consumed = self._feed(
                    bytes(
                        self._raw_queue
                    )
                )

                del self._raw_queue[
                    :consumed
                ]

                if not self._mccp_active:
                    return

                continue

            decompressor = self._decompressor

            if decompressor is None:
                raise TelnetProtocolError(
                    "MCCP active without decompressor"
                )

            compressed = bytes(
                self._raw_queue
            )

            self._raw_queue.clear()

            try:
                # Bound output *inside* zlib.  Checking len() only after an
                # unbounded decompress would be too late for a decompression
                # bomb.  Request one byte beyond the configured budget as a
                # sentinel; if zlib can produce it, reject the stream.
                decompressed = decompressor.decompress(
                    compressed,
                    MAX_MCCP_OUTPUT_PER_READ + 1,
                )

            except zlib.error as exc:
                # Never reinterpret failed compressed bytes as raw Telnet.
                raise TelnetProtocolError(
                    f"MCCP2 decompression failed: {exc}"
                ) from exc

            if len(decompressed) > MAX_MCCP_OUTPUT_PER_READ:
                raise TelnetProtocolError(
                    "MCCP2 decompressed output exceeds configured per-read limit"
                )

            if decompressed:
                consumed = self._feed(
                    decompressed
                )

                if consumed != len(
                    decompressed
                ):
                    # _feed can return early only when MCCP activates. It is
                    # already active here, so a second activation would mean
                    # corrupt/nested compression framing.
                    raise TelnetProtocolError(
                        "unexpected MCCP transition inside decompressed stream"
                    )

            if decompressor.eof:
                # MCCP2 normally persists to connection termination, but if the
                # zlib stream ends and trailing bytes exist, continue parsing
                # those trailing bytes as ordinary Telnet only after zlib has
                # explicitly identified them as unused input.
                trailing = decompressor.unused_data

                self._mccp_active = False
                self._decompressor = None

                if trailing:
                    self._raw_queue.extend(
                        trailing
                    )

    # ------------------------------------------------------------------
    # Telnet state machine
    # ------------------------------------------------------------------

    def _check_subnegotiation_size(self) -> None:
        limit = _subnegotiation_limit(self._sb_option)
        if len(self._sb_buffer) > limit:
            option = self._sb_option
            raise TelnetProtocolError(
                f"Telnet subnegotiation for option {option} exceeds "
                f"configured limit of {limit} bytes"
            )

    def _feed(
        self,
        chunk: bytes,
    ) -> int:
        """
        Feed Telnet bytes.

        Returns the number of bytes consumed.

        If MCCP2 activates mid-chunk, parsing stops immediately after the
        activation marker so the remaining bytes can be reinterpreted by the
        decompressor.
        """

        text_run = bytearray()

        def flush_text() -> None:
            if not text_run:
                return

            self._pending_text_bytes.extend(
                text_run
            )

            text_run.clear()

            if (
                len(
                    self._pending_text_bytes
                )
                > MAX_PENDING_TEXT_BYTES
            ):
                raise TelnetProtocolError(
                    "pending text exceeds configured limit"
                )

            self._emit_text()

        index = 0
        length = len(
            chunk
        )

        while index < length:
            byte = chunk[index]
            index += 1

            state = self._state

            # ----------------------------------------------------------
            # DATA
            # ----------------------------------------------------------

            if state == _TelnetState.DATA:
                if byte == IAC:
                    flush_text()

                    self._state = (
                        _TelnetState.IAC_SEEN
                    )

                else:
                    text_run.append(
                        byte
                    )

            # ----------------------------------------------------------
            # IAC command
            # ----------------------------------------------------------

            elif state == _TelnetState.IAC_SEEN:
                if byte == IAC:
                    text_run.append(
                        IAC
                    )

                    self._state = (
                        _TelnetState.DATA
                    )

                elif byte in (
                    WILL,
                    WONT,
                    DO,
                    DONT,
                ):
                    self._pending_cmd = byte

                    self._state = (
                        _TelnetState.NEGOTIATING
                    )

                elif byte == SB:
                    self._sb_buffer.clear()
                    self._sb_option = None

                    self._state = (
                        _TelnetState.SUBNEG
                    )

                elif byte in (
                    GA,
                    EOR_MARK,
                ):
                    # Text preceding the marker belongs to PROMPT rather than
                    # TEXT. text_run should generally already be empty because
                    # DATA flushes before entering IAC_SEEN, but retaining this
                    # guard makes the framing invariant explicit.
                    if text_run:
                        self._pending_text_bytes.extend(
                            text_run
                        )

                        text_run.clear()

                    self._emit_prompt()

                    self._state = (
                        _TelnetState.DATA
                    )

                else:
                    # NOP and unsupported one-byte commands are ignored.
                    self._state = (
                        _TelnetState.DATA
                    )

            # ----------------------------------------------------------
            # WILL/WONT/DO/DONT option byte
            # ----------------------------------------------------------

            elif state == _TelnetState.NEGOTIATING:
                command = self._pending_cmd
                self._pending_cmd = None

                if command is None:
                    raise TelnetProtocolError(
                        "negotiation state without pending command"
                    )

                self._handle_negotiation(
                    command,
                    byte,
                )

                self._state = (
                    _TelnetState.DATA
                )

            # ----------------------------------------------------------
            # Subnegotiation
            # ----------------------------------------------------------

            elif state == _TelnetState.SUBNEG:
                if self._sb_option is None:
                    self._sb_option = byte

                elif byte == IAC:
                    self._state = (
                        _TelnetState.SUBNEG_IAC
                    )

                else:
                    self._sb_buffer.append(
                        byte
                    )

                    self._check_subnegotiation_size()

            elif state == _TelnetState.SUBNEG_IAC:
                if byte == SE:
                    option = self._sb_option

                    payload = bytes(
                        self._sb_buffer
                    )

                    self._sb_buffer.clear()
                    self._sb_option = None

                    self._state = (
                        _TelnetState.DATA
                    )

                    self._handle_subnegotiation(
                        option,
                        payload,
                    )

                elif byte == IAC:
                    # Escaped literal IAC inside SB payload.
                    self._sb_buffer.append(
                        IAC
                    )

                    self._state = (
                        _TelnetState.SUBNEG
                    )

                else:
                    # Malformed IAC <byte> inside a subnegotiation.
                    #
                    # Preserve the byte and continue collecting instead of
                    # accidentally terminating or exposing payload as text.
                    self._sb_buffer.append(
                        byte
                    )

                    self._state = (
                        _TelnetState.SUBNEG
                    )

                self._check_subnegotiation_size()

            # ----------------------------------------------------------
            # MCCP transition
            # ----------------------------------------------------------

            if self._mccp_just_activated:
                self._mccp_just_activated = False

                flush_text()

                return index

        flush_text()

        return length

    # ------------------------------------------------------------------
    # Text decoding
    # ------------------------------------------------------------------

    def _emit_text(self) -> None:
        if not self._pending_text_bytes:
            return

        data = bytes(
            self._pending_text_bytes
        )

        text, consumed = _decode_text_prefix(
            data,
            self.encoding,
        )

        if text:
            self.bus.emit(
                EventType.TEXT,
                text,
            )

        self._pending_text_bytes = bytearray(
            data[consumed:]
        )

    def _emit_prompt(self) -> None:
        """
        Flush all bytes preceding a Telnet prompt marker.

        A GA/EOR boundary is authoritative: bytes before it cannot legitimately
        depend on future bytes after it. Any incomplete character is therefore
        replaced rather than carried into the next record.
        """

        data = bytes(
            self._pending_text_bytes
        )

        self._pending_text_bytes.clear()

        text = data.decode(
            self.encoding,
            errors="replace",
        )

        self.bus.emit(
            EventType.PROMPT,
            text,
        )

    def _emit_final_text(self) -> None:
        if not self._pending_text_bytes:
            return

        data = bytes(
            self._pending_text_bytes
        )

        self._pending_text_bytes.clear()

        text = data.decode(
            self.encoding,
            errors="replace",
        )

        if text:
            self.bus.emit(
                EventType.TEXT,
                text,
            )

    # ------------------------------------------------------------------
    # Option negotiation
    # ------------------------------------------------------------------

    def _handle_negotiation(
        self,
        command: int,
        option: int,
    ) -> None:
        name = _CMD_NAMES.get(
            command,
            str(command),
        )

        logger.debug(
            "telnet %s %d",
            name,
            option,
        )

        if not self._negotiation_allowed(command, option):
            return

        # --------------------------------------------------------------
        # Server WILL option
        # --------------------------------------------------------------

        if command == WILL:
            accepted = option in {
                OPT_SGA,
                OPT_ECHO,
                OPT_EOR,
                OPT_MCCP2,
                OPT_GMCP,
                OPT_MSDP,
            }

            if accepted:
                newly_enabled = option not in self._remote_options
                self._remote_options.add(option)
                self._refresh_enabled_options()

                # Telnet negotiation is state-based. Re-acknowledging a WILL
                # for an option already enabled can itself sustain a WILL/DO
                # loop, so acknowledge only the transition into enabled.
                if newly_enabled:
                    self._send_telnet_command(IAC, DO, option)
                    self.bus.emit(
                        EventType.OPTION_CHANGE,
                        {"option": option, "state": "will"},
                    )
            else:
                self._send_telnet_command(IAC, DONT, option)
            return

        # --------------------------------------------------------------
        # Server WONT option
        # --------------------------------------------------------------

        if command == WONT:
            changed = option in self._remote_options
            self._remote_options.discard(option)
            self._refresh_enabled_options()

            if option == OPT_MCCP2:
                self._mccp_active = False
                self._decompressor = None

            if changed:
                self.bus.emit(
                    EventType.OPTION_CHANGE,
                    {"option": option, "state": "wont"},
                )
            return

        # --------------------------------------------------------------
        # Server asks client DO option
        # --------------------------------------------------------------

        if command == DO:
            if option in {OPT_TTYPE, OPT_NAWS, OPT_SGA}:
                newly_enabled = option not in self._local_options
                self._local_options.add(option)
                self._refresh_enabled_options()

                if newly_enabled:
                    self._send_telnet_command(IAC, WILL, option)
                    if option == OPT_NAWS:
                        self.send_naws(self.cols, self.rows)
                    self.bus.emit(
                        EventType.OPTION_CHANGE,
                        {"option": option, "state": "do"},
                    )
                return

            self._send_telnet_command(IAC, WONT, option)
            return

        # --------------------------------------------------------------
        # Server asks client DONT option
        # --------------------------------------------------------------

        if command == DONT:
            changed = option in self._local_options
            self._local_options.discard(option)
            self._refresh_enabled_options()

            if changed:
                self.bus.emit(
                    EventType.OPTION_CHANGE,
                    {"option": option, "state": "dont"},
                )

    def _negotiation_allowed(
        self,
        command: int,
        option: int,
    ) -> bool:
        """Bound pathological negotiation churn without blocking the session.

        The first messages in a generous rolling window are handled normally.
        Once a peer exceeds that budget, negotiation for only that option is
        ignored for a short cooling-off period; ordinary text and other Telnet
        options continue flowing. A single diagnostic notice is emitted when
        suppression starts.
        """

        now = time.monotonic()
        suppressed_until = self._negotiation_suppressed_until.get(option, 0.0)

        if suppressed_until > now:
            return False

        if suppressed_until:
            self._negotiation_suppressed_until.pop(option, None)
            self._negotiation_times.pop(option, None)

        history = self._negotiation_times.setdefault(option, deque())
        cutoff = now - NEGOTIATION_CHURN_WINDOW_SECONDS
        while history and history[0] < cutoff:
            history.popleft()
        history.append(now)

        if len(history) <= MAX_NEGOTIATIONS_PER_OPTION_WINDOW:
            return True

        self._negotiation_suppressed_until[option] = (
            now + NEGOTIATION_CHURN_SUPPRESS_SECONDS
        )

        notice = {
            "kind": "negotiation-churn",
            "option": option,
            "command": _CMD_NAMES.get(command, str(command)),
            "count": len(history),
            "window_seconds": NEGOTIATION_CHURN_WINDOW_SECONDS,
            "suppressed_seconds": NEGOTIATION_CHURN_SUPPRESS_SECONDS,
        }
        self.bus.emit(EventType.PROTOCOL_NOTICE, notice)
        logger.warning(
            "suppressing Telnet negotiation churn for option %d for %.1fs",
            option,
            NEGOTIATION_CHURN_SUPPRESS_SECONDS,
        )
        return False

    def _refresh_enabled_options(
        self,
    ) -> None:
        self.options_enabled = (
            self._remote_options
            | self._local_options
        )

    # ------------------------------------------------------------------
    # Subnegotiation
    # ------------------------------------------------------------------

    def _handle_subnegotiation(
        self,
        option: Optional[int],
        payload: bytes,
    ) -> None:
        if option is None:
            raise TelnetProtocolError(
                "subnegotiation ended without option byte"
            )

        if option == OPT_TTYPE:
            self._handle_ttype(
                payload
            )

        elif option == OPT_MCCP2:
            self._handle_mccp2(
                payload
            )

        elif option == OPT_GMCP:
            self._handle_gmcp(
                payload
            )

        elif option == OPT_MSDP:
            self._handle_msdp(
                payload
            )

        else:
            logger.debug(
                "unhandled subnegotiation for option %s (%d bytes)",
                option,
                len(payload),
            )

    def _handle_ttype(
        self,
        payload: bytes,
    ) -> None:
        # RFC 1091:
        #   SEND = 1
        #   IS   = 0
        if payload[:1] != b"\x01":
            return

        terminal = self.terminal_type.encode(
            "ascii",
            errors="replace",
        )

        response = (
            bytes(
                [
                    0,
                ]
            )
            + terminal
        )

        self._send_subnegotiation(
            OPT_TTYPE,
            response,
        )

    def _handle_mccp2(
        self,
        payload: bytes,
    ) -> None:
        # MCCP2 activation marker is expected to be empty.
        if payload:
            logger.debug(
                "MCCP2 activation marker carried %d unexpected payload bytes",
                len(payload),
            )

        if self._mccp_active:
            raise TelnetProtocolError(
                "duplicate MCCP2 activation"
            )

        self._decompressor = (
            zlib.decompressobj()
        )

        self._mccp_active = True
        self._mccp_just_activated = True

        logger.info(
            "MCCP2 compression active"
        )

    def _handle_gmcp(
        self,
        payload: bytes,
    ) -> None:
        if len(payload) > MAX_GMCP_BYTES:
            raise TelnetProtocolError(
                "GMCP payload exceeds configured limit"
            )

        text = payload.decode(
            "utf-8",
            errors="replace",
        )

        if not text:
            return

        if " " in text:
            package, _, json_part = text.partition(
                " "
            )

            max_depth = 0
            depth = 0
            in_string = False
            escaped = False
            for char in json_part:
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False
                    continue

                if char == '"':
                    in_string = True
                    continue

                if char in "[{":
                    depth += 1
                    if depth > max_depth:
                        max_depth = depth
                elif char in "]}":
                    depth = max(0, depth - 1)

            try:
                if max_depth > 1024:
                    raise ValueError("GMCP JSON nesting too deep")
                data = json.loads(json_part)

            except (json.JSONDecodeError, RecursionError, ValueError):
                # Some MUDs emit non-JSON GMCP values.  Pathologically deep
                # JSON can also produce an enormous nested Python structure
                # or overflow the decoder before returning.  Preserve either
                # form as bounded text rather than letting one remote message
                # tear down the read loop.
                data = json_part

        else:
            package = text
            data = None

        if not package:
            return

        self.bus.emit(
            EventType.GMCP,
            {
                "package": package,
                "data": data,
            },
        )

    def _handle_msdp(
        self,
        payload: bytes,
    ) -> None:
        if len(payload) > MAX_MSDP_BYTES:
            raise TelnetProtocolError(
                "MSDP payload exceeds configured limit"
            )

        try:
            parsed = _parse_msdp(
                payload
            )

        except Exception:
            logger.exception(
                "failed to parse MSDP payload"
            )

            return

        for variable, value in parsed.items():
            self.bus.emit(
                EventType.MSDP,
                {
                    "variable": variable,
                    "value": value,
                },
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _escape_iac(
    data: bytes,
) -> bytes:
    """
    Escape literal Telnet IAC bytes in DATA/subnegotiation payloads.

    RFC 854 represents a literal 0xFF as:

        IAC IAC
    """

    return data.replace(
        bytes(
            [
                IAC,
            ]
        ),
        bytes(
            [
                IAC,
                IAC,
            ]
        ),
    )


def _decode_utf8_prefix(
    data: bytes,
) -> tuple[str, int]:
    """
    Compatibility helper retained for callers/tests expecting the original
    UTF-8-specific function.
    """

    return _decode_text_prefix(
        data,
        "utf-8",
    )


def _decode_text_prefix(
    data: bytes,
    encoding: str,
) -> tuple[str, int]:
    """Decode all bytes that are safe to consume now.

    The incremental codec is used with ``errors="replace"`` so malformed
    *complete* byte sequences can never wedge the receive buffer, while a
    trailing incomplete multibyte character remains buffered for the next
    socket read.  This distinction matters when malformed bytes occur *before*
    a valid character split across reads: replacing the malformed byte must
    not force replacement of the incomplete trailing character too.

    The returned integer is the number of input bytes consumed.
    """

    if not data:
        return "", 0

    decoder_factory = codecs.getincrementaldecoder(encoding)
    decoder = decoder_factory(errors="replace")
    text = decoder.decode(data, final=False)

    state = decoder.getstate()
    pending = state[0] if isinstance(state, tuple) and state else b""
    if not isinstance(pending, (bytes, bytearray)):
        # Python's standard incremental byte decoders expose pending bytes as
        # state[0].  A third-party codec with a different state shape should
        # fail safe by consuming what it already decoded rather than retaining
        # an unknowable amount of input forever.
        pending = b""

    consumed = len(data) - len(pending)
    if consumed < 0 or consumed > len(data):
        consumed = len(data)

    return text, consumed


# ---------------------------------------------------------------------------
# MSDP
# ---------------------------------------------------------------------------


def _parse_msdp(
    payload: bytes,
) -> dict[str, Any]:
    """
    Parse an MSDP body into nested Python structures.

    Supports:
      - VAR / VAL pairs
      - TABLE
      - ARRAY

    The parser is intentionally defensive. Malformed closures end at the
    payload boundary rather than indexing beyond it.
    """

    position = 0
    length = len(
        payload
    )

    def read_until(
        stop_bytes: set[int],
    ) -> str:
        nonlocal position

        start = position

        while (
            position < length
            and payload[position]
            not in stop_bytes
        ):
            position += 1

        return payload[
            start:position
        ].decode(
            "utf-8",
            errors="replace",
        )

    def parse_value(depth: int = 0) -> Any:
        nonlocal position

        if depth > MAX_MSDP_NESTING:
            raise ValueError(
                f"MSDP nesting exceeds configured limit of {MAX_MSDP_NESTING}"
            )

        if position >= length:
            return ""

        # --------------------------------------------------------------
        # Array
        # --------------------------------------------------------------

        if (
            payload[position]
            == MSDP_ARRAY_OPEN
        ):
            position += 1

            items: list[Any] = []

            while (
                position < length
                and payload[position]
                != MSDP_ARRAY_CLOSE
            ):
                if (
                    payload[position]
                    == MSDP_VAL
                ):
                    position += 1

                    if position >= length:
                        items.append("")
                        break

                items.append(
                    parse_value(depth + 1)
                )

            if (
                position < length
                and payload[position]
                == MSDP_ARRAY_CLOSE
            ):
                position += 1

            return items

        # --------------------------------------------------------------
        # Table
        # --------------------------------------------------------------

        if (
            payload[position]
            == MSDP_TABLE_OPEN
        ):
            position += 1

            table: dict[
                str,
                Any,
            ] = {}

            while (
                position < length
                and payload[position]
                != MSDP_TABLE_CLOSE
            ):
                if (
                    payload[position]
                    != MSDP_VAR
                ):
                    # Skip malformed/unknown byte while guaranteeing progress.
                    position += 1
                    continue

                position += 1

                name = read_until(
                    {
                        MSDP_VAL,
                        MSDP_TABLE_CLOSE,
                    }
                )

                if (
                    position >= length
                    or payload[position]
                    != MSDP_VAL
                ):
                    table[name] = ""
                    continue

                position += 1

                table[name] = (
                    parse_value(depth + 1)
                )

            if (
                position < length
                and payload[position]
                == MSDP_TABLE_CLOSE
            ):
                position += 1

            return table

        # --------------------------------------------------------------
        # Scalar
        # --------------------------------------------------------------

        return read_until(
            {
                MSDP_VAR,
                MSDP_VAL,
                MSDP_TABLE_CLOSE,
                MSDP_ARRAY_CLOSE,
            }
        )

    result: dict[
        str,
        Any,
    ] = {}

    while position < length:
        if (
            payload[position]
            != MSDP_VAR
        ):
            position += 1
            continue

        position += 1

        name = read_until(
            {
                MSDP_VAL,
            }
        )

        if (
            position >= length
            or payload[position]
            != MSDP_VAL
        ):
            result[name] = ""
            continue

        position += 1

        result[name] = (
            parse_value()
        )

    return result
