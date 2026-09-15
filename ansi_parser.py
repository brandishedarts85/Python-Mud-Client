"""
ansi_parser.py -- the renderer.

Turns raw text (as delivered by client_core's TEXT/PROMPT events) into a
UI-agnostic sequence of styled segments: plain strings tagged with a Style
describing color/attributes. Any UI layer (Qt, Textual, a browser via HTML,
a curses screen) consumes StyledLine objects and draws them however it likes;
this module never touches a widget.

Covers:
  - classic ANSI SGR: 16-color, bold/dim/italic/underline/blink/reverse/strike
  - xterm 256-color (38;5;n / 48;5;n)
  - truecolor (38;2;r;g;b / 48;2;r;g;b)
  - colon-form extended color where practical (e.g. 38:2:r:g:b)
  - reset codes and combined SGR sequences
  - CR/LF and bare CR normalization
  - safe recognition/discard of unsupported CSI/OSC/ESC sequences
  - split escape sequences across feed() calls
  - bounded scrollback

Design rule:
    recognize broadly, interpret narrowly, discard safely.

Only SGR meaning is interpreted. Other terminal control sequences are consumed
without being exposed as visible text.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from typing import Deque, Optional


# xterm 256-color palette (0-15 match the standard 16 ANSI colors)
_XTERM_256: Optional[list[tuple[int, int, int]]] = None

# The 16 base ANSI colors as RGB, used for SGR 30-37/40-47/90-97/100-107.
_BASE16 = [
    (0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0),
    (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229),
    (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0),
    (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
]

# Conservative caps for defensive parsing.
_MAX_CSI_LEN = 128
_MAX_OSC_LEN = 4096
_MAX_SGR_PARAMS = 64
MAX_LOGICAL_LINE_CHARS = 1024 * 1024


class AnsiParseLimitError(ValueError):
    """Raised when one logical terminal line exceeds defensive bounds."""


@dataclass(frozen=True)
class Style:
    fg: Optional[tuple[int, int, int]] = None
    bg: Optional[tuple[int, int, int]] = None
    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: bool = False
    blink: bool = False
    reverse: bool = False
    strike: bool = False

    def merged(self, **changes) -> "Style":
        return replace(self, **changes)


DEFAULT_STYLE = Style()


@dataclass(frozen=True)
class Segment:
    text: str
    style: Style


@dataclass
class StyledLine:
    segments: list[Segment] = field(default_factory=list)

    def plain_text(self) -> str:
        return "".join(segment.text for segment in self.segments)


def _xterm256() -> list[tuple[int, int, int]]:
    global _XTERM_256
    if _XTERM_256 is not None:
        return _XTERM_256

    palette = list(_BASE16)

    steps = [0, 95, 135, 175, 215, 255]
    for r in range(6):
        for g in range(6):
            for b in range(6):
                palette.append((steps[r], steps[g], steps[b]))

    for i in range(24):
        value = 8 + i * 10
        palette.append((value, value, value))

    _XTERM_256 = palette
    return palette


class AnsiParser:
    """
    Stateful ANSI-to-styled-text parser.

    feed() accepts already-decoded Unicode text. Chunks may split escape
    sequences or CRLF pairs; parser state is retained between calls.

    Completed newline-terminated lines are returned from feed().
    current_line() exposes the in-progress line.
    flush_line() is intended for telnet GA/EOR prompt boundaries.
    """

    def __init__(self, *, max_line_chars: int = MAX_LOGICAL_LINE_CHARS) -> None:
        if max_line_chars <= 0:
            raise ValueError("max_line_chars must be greater than zero")
        self.max_line_chars = int(max_line_chars)
        self._style = DEFAULT_STYLE
        self._buffer = ""
        self._current = StyledLine()
        self._current_chars = 0
        self._discard_control: Optional[str] = None
        self._discard_saw_esc = False

    def feed(self, text: str) -> list[StyledLine]:
        if not text:
            return []

        # Oversized string/CSI controls are discarded across feed() boundaries
        # until their real terminator arrives.  Without this state, dropping
        # only the leading ESC would expose the remainder as visible text and
        # could feed control payloads into triggers.
        if self._discard_control is not None:
            text = self._discard_control_prefix(text)
            if not text:
                return []

        self._buffer += text
        completed: list[StyledLine] = []

        buf = self._buffer
        n = len(buf)
        i = 0
        plain_start = 0
        hold_from: Optional[int] = None

        def flush_plain(end: int) -> None:
            nonlocal plain_start
            if end <= plain_start:
                return
            chunk = buf[plain_start:end]
            if chunk:
                self._append_segment(chunk, self._style)
            plain_start = end

        while i < n:
            ch = buf[i]

            if ch == "\x1b":
                flush_plain(i)
                consumed = self._consume_escape(buf, i)

                if consumed is None:
                    hold_from = i
                    break

                i = consumed
                plain_start = i
                continue

            if ch == "\r":
                if i + 1 >= n:
                    flush_plain(i)
                    hold_from = i
                    break

                flush_plain(i)
                i += 1

                if i < n and buf[i] == "\n":
                    i += 1

                plain_start = i
                completed.append(self._current)
                self._current = StyledLine()
                self._current_chars = 0
                continue

            if ch == "\n":
                flush_plain(i)
                i += 1
                plain_start = i
                completed.append(self._current)
                self._current = StyledLine()
                self._current_chars = 0
                continue

            i += 1

        if hold_from is not None:
            flush_plain(hold_from)
            self._buffer = buf[hold_from:]
        else:
            flush_plain(n)
            self._buffer = ""

        return completed

    def current_line(self) -> StyledLine:
        return self._current

    def flush_line(self) -> Optional[StyledLine]:
        """
        End the current logical line without requiring CR/LF.

        Intended for telnet GA/EOR prompt boundaries. Buffered partial control
        sequences are intentionally retained until more bytes arrive; they are
        not exposed as visible prompt text.
        """
        if not self._current.segments:
            return None

        line = self._current
        self._current = StyledLine()
        self._current_chars = 0
        return line

    def reset(self) -> None:
        """Reset parser style, partial-control state, and current line."""
        self._style = DEFAULT_STYLE
        self._buffer = ""
        self._current = StyledLine()
        self._current_chars = 0
        self._discard_control = None
        self._discard_saw_esc = False

    def _append_segment(self, text: str, style: Style) -> None:
        """Append text while coalescing adjacent segments with equal style."""
        if not text:
            return
        if self._current_chars + len(text) > self.max_line_chars:
            raise AnsiParseLimitError(
                f"logical ANSI line exceeds configured limit of {self.max_line_chars} characters"
            )
        self._current_chars += len(text)
        if self._current.segments and self._current.segments[-1].style == style:
            previous = self._current.segments[-1]
            self._current.segments[-1] = Segment(previous.text + text, style)
        else:
            self._current.segments.append(Segment(text, style))

    # ------------------------------------------------------------------
    # Escape-sequence scanning
    # ------------------------------------------------------------------

    def _consume_escape(self, buf: str, start: int) -> Optional[int]:
        """
        Consume one escape/control sequence beginning at start.

        Returns the index immediately after the consumed sequence.
        Returns None when the sequence appears incomplete and should be held for
        the next feed() call.

        Unknown-but-complete ESC forms are discarded safely.
        """
        n = len(buf)
        if start + 1 >= n:
            return None

        leader = buf[start + 1]

        if leader == "[":
            return self._consume_csi(buf, start)

        if leader == "]":
            return self._consume_osc(buf, start)

        # DCS, SOS, PM, APC: string controls terminated by ST (ESC \).
        if leader in ("P", "X", "^", "_"):
            return self._consume_st_string(buf, start)

        # Two-byte Fe-style escape sequence: ESC followed by a final byte.
        # This safely consumes common sequences such as ESC 7 / ESC 8 / ESC c.
        code = ord(leader)
        if 0x30 <= code <= 0x7E:
            return start + 2

        # Intermediate-byte escape sequences may contain 0x20-0x2F followed by
        # a final 0x30-0x7E. Scan only a very small bounded envelope.
        i = start + 1
        limit = min(n, start + 16)
        while i < limit and 0x20 <= ord(buf[i]) <= 0x2F:
            i += 1

        if i < n and 0x30 <= ord(buf[i]) <= 0x7E:
            return i + 1

        # Invalid ESC byte followed by ordinary data: discard only ESC itself
        # so visible text is not trapped indefinitely.
        return start + 1

    def _consume_csi(self, buf: str, start: int) -> Optional[int]:
        """
        Parse the ANSI CSI syntactic envelope.

        CSI is:
            ESC [
            parameter bytes     0x30-0x3F
            intermediate bytes  0x20-0x2F
            final byte          0x40-0x7E

        Only final 'm' (SGR) is interpreted. Everything else is discarded.
        """
        n = len(buf)
        i = start + 2

        if i >= n:
            return None

        max_end = min(n, start + _MAX_CSI_LEN)

        param_start = i
        while i < max_end and 0x30 <= ord(buf[i]) <= 0x3F:
            i += 1
        params = buf[param_start:i]

        while i < max_end and 0x20 <= ord(buf[i]) <= 0x2F:
            i += 1

        if i >= n:
            if n - start < _MAX_CSI_LEN:
                return None
            self._discard_control = "csi"
            self._discard_saw_esc = False
            return n

        if i >= max_end:
            # The syntactic envelope exceeded our CSI budget.  If the final
            # byte is already present later in this same chunk, discard through
            # it and resume immediately.  Otherwise enter cross-feed discard
            # mode until a final byte arrives.
            for pos in range(max_end, n):
                if 0x40 <= ord(buf[pos]) <= 0x7E:
                    return pos + 1
            self._discard_control = "csi"
            self._discard_saw_esc = False
            return n

        final = buf[i]
        if not (0x40 <= ord(final) <= 0x7E):
            # Malformed CSI. Drop only the ESC and allow the rest to be
            # reconsidered as ordinary input instead of stalling the stream.
            return start + 1

        if final == "m":
            self._apply_sgr(params)

        return i + 1

    def _consume_osc(self, buf: str, start: int) -> Optional[int]:
        """
        Consume Operating System Command (OSC).

        OSC terminates with BEL or ST (ESC \\). It is never rendered or
        interpreted by this module.
        """
        n = len(buf)
        i = start + 2
        limit = min(n, start + _MAX_OSC_LEN)

        while i < limit:
            ch = buf[i]
            if ch == "\x07":  # BEL terminator
                return i + 1
            if ch == "\x1b":
                if i + 1 >= n:
                    return None
                if buf[i + 1] == "\\":
                    return i + 2
            i += 1

        if limit == n and n - start < _MAX_OSC_LEN:
            return None

        # Pathological unterminated OSC.  Continue looking for the real
        # terminator in the rest of this chunk; if it is not present, discard
        # subsequent chunks until BEL/ST rather than exposing OSC contents as
        # ordinary MUD text.
        i = limit
        while i < n:
            if buf[i] == "\x07":
                return i + 1
            if buf[i] == "\x1b" and i + 1 < n and buf[i + 1] == "\\":
                return i + 2
            i += 1
        self._discard_control = "osc"
        self._discard_saw_esc = bool(n and buf[-1] == "\x1b")
        return n

    def _consume_st_string(self, buf: str, start: int) -> Optional[int]:
        """Consume DCS/SOS/PM/APC strings, terminated by ST (ESC \\)."""
        n = len(buf)
        i = start + 2
        limit = min(n, start + _MAX_OSC_LEN)

        while i < limit:
            if buf[i] == "\x1b":
                if i + 1 >= n:
                    return None
                if buf[i + 1] == "\\":
                    return i + 2
            i += 1

        if limit == n and n - start < _MAX_OSC_LEN:
            return None

        i = limit
        while i < n:
            if buf[i] == "\x1b" and i + 1 < n and buf[i + 1] == "\\":
                return i + 2
            i += 1
        self._discard_control = "st"
        self._discard_saw_esc = bool(n and buf[-1] == "\x1b")
        return n

    def _discard_control_prefix(self, text: str) -> str:
        """Discard bytes belonging to a previously oversized control string.

        Return only text after the terminating byte/sequence.  The small
        ``_discard_saw_esc`` flag handles an ST terminator split exactly between
        two feed() calls.
        """
        mode = self._discard_control
        if mode is None:
            return text

        if mode == "csi":
            for index, ch in enumerate(text):
                if 0x40 <= ord(ch) <= 0x7E:
                    self._discard_control = None
                    self._discard_saw_esc = False
                    return text[index + 1 :]
            return ""

        for index, ch in enumerate(text):
            if mode == "osc" and ch == "\x07":
                self._discard_control = None
                self._discard_saw_esc = False
                return text[index + 1 :]

            if self._discard_saw_esc and ch == "\\":
                self._discard_control = None
                self._discard_saw_esc = False
                return text[index + 1 :]

            self._discard_saw_esc = ch == "\x1b"

        return ""

    # ------------------------------------------------------------------
    # SGR
    # ------------------------------------------------------------------

    def _apply_sgr(self, params: str) -> None:
        if params == "":
            self._style = DEFAULT_STYLE
            return

        tokens = self._tokenize_sgr(params)
        if tokens is None:
            return

        style = self._style
        idx = 0

        while idx < len(tokens):
            token = tokens[idx]

            if isinstance(token, tuple):
                kind = token[0]
                if kind == "fg":
                    style = style.merged(fg=token[1])
                elif kind == "bg":
                    style = style.merged(bg=token[1])
                idx += 1
                continue

            code = token

            if code == 0:
                style = DEFAULT_STYLE
            elif code == 1:
                style = style.merged(bold=True)
            elif code == 2:
                style = style.merged(dim=True)
            elif code == 3:
                style = style.merged(italic=True)
            elif code == 4:
                style = style.merged(underline=True)
            elif code in (5, 6):
                style = style.merged(blink=True)
            elif code == 7:
                style = style.merged(reverse=True)
            elif code == 9:
                style = style.merged(strike=True)
            elif code == 22:
                style = style.merged(bold=False, dim=False)
            elif code == 23:
                style = style.merged(italic=False)
            elif code == 24:
                style = style.merged(underline=False)
            elif code == 25:
                style = style.merged(blink=False)
            elif code == 27:
                style = style.merged(reverse=False)
            elif code == 29:
                style = style.merged(strike=False)
            elif 30 <= code <= 37:
                style = style.merged(fg=_BASE16[code - 30])
            elif code == 39:
                style = style.merged(fg=None)
            elif 40 <= code <= 47:
                style = style.merged(bg=_BASE16[code - 40])
            elif code == 49:
                style = style.merged(bg=None)
            elif 90 <= code <= 97:
                style = style.merged(fg=_BASE16[8 + code - 90])
            elif 100 <= code <= 107:
                style = style.merged(bg=_BASE16[8 + code - 100])
            elif code in (38, 48):
                rgb, consumed = self._parse_extended_color_tokens(tokens, idx)
                if rgb is not None:
                    if code == 38:
                        style = style.merged(fg=rgb)
                    else:
                        style = style.merged(bg=rgb)
                idx = max(idx + 1, consumed + 1)
                continue

            idx += 1

        self._style = style

    @staticmethod
    def _tokenize_sgr(
        params: str,
    ) -> Optional[list[int | tuple[str, tuple[int, int, int]]]]:
        """
        Tokenize SGR parameters.

        Semicolon syntax is left as integers so 38/48 can consume following
        parameters. Colon-form extended colors are normalized into synthetic
        ('fg'/'bg', rgb) tokens.

        Unknown/malformed colon groups are ignored rather than changing style.
        """
        raw_parts = params.split(";")
        if len(raw_parts) > _MAX_SGR_PARAMS:
            return None

        tokens: list[int | tuple[str, tuple[int, int, int]]] = []

        for raw in raw_parts:
            if ":" not in raw:
                if raw == "":
                    tokens.append(0)
                    continue
                try:
                    tokens.append(int(raw))
                except ValueError:
                    continue
                continue

            fields = raw.split(":")
            try:
                lead = int(fields[0]) if fields[0] else 0
            except ValueError:
                continue

            # Common colon forms:
            #   38:5:n
            #   48:5:n
            #   38:2:r:g:b
            #   38:2::r:g:b  (optional colorspace id omitted)
            if lead not in (38, 48) or len(fields) < 3:
                continue

            mode = fields[1]
            rgb: Optional[tuple[int, int, int]] = None

            if mode == "5" and len(fields) >= 3:
                try:
                    palette_index = int(fields[2])
                except ValueError:
                    continue
                palette = _xterm256()
                if 0 <= palette_index < len(palette):
                    rgb = palette[palette_index]

            elif mode == "2":
                components = fields[2:]
                if components and components[0] == "":
                    components = components[1:]
                if len(components) < 3:
                    continue
                try:
                    r, g, b = (int(components[0]), int(components[1]), int(components[2]))
                except ValueError:
                    continue
                if all(0 <= value <= 255 for value in (r, g, b)):
                    rgb = (r, g, b)

            if rgb is not None:
                tokens.append(("fg" if lead == 38 else "bg", rgb))

        return tokens

    @staticmethod
    def _parse_extended_color_tokens(
        tokens: list[int | tuple[str, tuple[int, int, int]]],
        idx: int,
    ) -> tuple[Optional[tuple[int, int, int]], int]:
        """
        Parse semicolon-form extended colors beginning at tokens[idx] == 38/48.

        Invalid requests leave the existing color unchanged.
        """
        if idx + 1 >= len(tokens) or not isinstance(tokens[idx + 1], int):
            return None, idx

        mode = tokens[idx + 1]

        if mode == 5:
            if idx + 2 >= len(tokens) or not isinstance(tokens[idx + 2], int):
                return None, idx + 1

            palette_index = tokens[idx + 2]
            palette = _xterm256()
            if 0 <= palette_index < len(palette):
                return palette[palette_index], idx + 2
            return None, idx + 2

        if mode == 2:
            if idx + 4 >= len(tokens):
                return None, idx + 1

            values = tokens[idx + 2: idx + 5]
            if not all(isinstance(value, int) for value in values):
                return None, idx + 1

            r, g, b = values
            if all(0 <= value <= 255 for value in (r, g, b)):
                return (r, g, b), idx + 4
            return None, idx + 4

        return None, idx + 1


class Scrollback:
    """Bounded ring buffer of StyledLine with simple substring search."""

    def __init__(self, max_lines: int = 10_000) -> None:
        if max_lines <= 0:
            raise ValueError("max_lines must be greater than zero")
        self.max_lines = max_lines
        self._lines: Deque[StyledLine] = deque(maxlen=max_lines)

    def append(self, line: StyledLine) -> None:
        self._lines.append(line)

    def set_max_lines(self, max_lines: int) -> None:
        """Resize the ring buffer while preserving the newest retained lines."""
        if max_lines <= 0:
            raise ValueError("max_lines must be greater than zero")
        if max_lines == self.max_lines:
            return
        retained = list(self._lines)[-max_lines:]
        self.max_lines = max_lines
        self._lines = deque(retained, maxlen=max_lines)

    def extend(self, lines: list[StyledLine]) -> None:
        self._lines.extend(lines)

    def __len__(self) -> int:
        return len(self._lines)

    def __getitem__(self, idx):
        # Preserve simple list-like indexing/slicing for callers.
        if isinstance(idx, slice):
            return list(self._lines)[idx]
        return self._lines[idx]

    def search(self, needle: str, case_sensitive: bool = False) -> list[int]:
        if not case_sensitive:
            needle = needle.casefold()

        hits: list[int] = []
        for i, line in enumerate(self._lines):
            text = line.plain_text()
            if not case_sensitive:
                text = text.casefold()
            if needle in text:
                hits.append(i)
        return hits

    def tail(self, n: int) -> list[StyledLine]:
        if n <= 0:
            return []
        lines = list(self._lines)
        return lines[-n:]
