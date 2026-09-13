"""
ansi_parser.py -- the renderer.

Turns raw text (as delivered by client_core's TEXT/PROMPT events) into a
UI-agnostic sequence of styled segments: plain strings tagged with a
Style describing color/attributes. Any UI layer (Qt, Textual, a browser
via HTML, a curses screen) consumes StyledLine objects and draws them
however it likes -- this module never touches a widget.

Covers:
  - classic ANSI SGR: 16-color, bold/dim/italic/underline/blink/reverse
  - xterm 256-color (38;5;n / 48;5;n)
  - truecolor (38;2;r;g;b / 48;2;r;g;b)
  - reset codes, combined SGR sequences (e.g. \x1b[1;32;44m)
  - CR/LF and bare CR normalization
  - a bounded scrollback buffer of finished lines

Non-SGR CSI sequences (cursor movement, clear screen, etc.) are
recognized and discarded by default -- most MUDs don't rely on them,
and a client that mis-renders cursor-addressed full-screen apps (rare
on MUDs) is far less annoying than one that leaks raw escape codes into
the scrollback.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

CSI_RE = re.compile(r"\x1b\[([0-9;:]*)([A-Za-z])")

# xterm 256-color palette (0-15 match the standard 16 ANSI colors)
_XTERM_256 = None  # built lazily, see _xterm256()


@dataclass(frozen=True)
class Style:
    fg: Optional[tuple[int, int, int]] = None   # RGB, None = default
    bg: Optional[tuple[int, int, int]] = None
    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: bool = False
    blink: bool = False
    reverse: bool = False
    strike: bool = False

    def merged(self, **changes) -> "Style":
        data = self.__dict__ | changes
        return Style(**data)


DEFAULT_STYLE = Style()


@dataclass
class Segment:
    text: str
    style: Style


@dataclass
class StyledLine:
    segments: list[Segment] = field(default_factory=list)

    def plain_text(self) -> str:
        return "".join(s.text for s in self.segments)


# The 16 base ANSI colors as RGB, used for SGR 30-37/40-47/90-97/100-107
_BASE16 = [
    (0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0),
    (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229),
    (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0),
    (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
]


def _xterm256() -> list[tuple[int, int, int]]:
    global _XTERM_256
    if _XTERM_256 is not None:
        return _XTERM_256
    palette = list(_BASE16)
    # 216-color cube (16-231)
    steps = [0, 95, 135, 175, 215, 255]
    for r in range(6):
        for g in range(6):
            for b in range(6):
                palette.append((steps[r], steps[g], steps[b]))
    # grayscale ramp (232-255)
    for i in range(24):
        v = 8 + i * 10
        palette.append((v, v, v))
    _XTERM_256 = palette
    return palette


class AnsiParser:
    """
    Stateful ANSI-to-styled-text parser. Feed it raw chunks of text
    (possibly split mid-escape-sequence across chunks -- that's fine,
    state persists across calls) and get back StyledLine objects for
    each completed line, plus the current in-progress line.
    """

    def __init__(self) -> None:
        self._style = DEFAULT_STYLE
        self._buffer = ""            # unterminated raw text since last flush
        self._current = StyledLine()
        self._pending_cr = False     # saw \r, waiting to see if \n follows

    def feed(self, text: str) -> list[StyledLine]:
        """Process a chunk of text (already UTF-8 decoded). Returns the
        list of lines that were completed by this chunk. Use current_line()
        to peek at the not-yet-terminated line (e.g. for a live prompt)."""
        text = unicodedata.normalize("NFC", text)
        self._buffer += text
        completed: list[StyledLine] = []

        i = 0
        buf = self._buffer
        n = len(buf)
        plain_start = i

        def flush_plain(end: int) -> None:
            if end > plain_start:
                chunk = buf[plain_start:end]
                if chunk:
                    self._current.segments.append(Segment(chunk, self._style))

        while i < n:
            ch = buf[i]
            if ch == "\x1b":
                # need at least the CSI intro; if the escape sequence is
                # split across chunks, stop here and wait for more data
                m = CSI_RE.match(buf, i)
                if m is None:
                    if n - i < 32:  # plausibly a truncated sequence
                        break
                    # not a recognizable escape at all; drop the ESC byte
                    flush_plain(i)
                    i += 1
                    plain_start = i
                    continue
                flush_plain(i)
                self._apply_csi(m.group(1), m.group(2))
                i = m.end()
                plain_start = i
                continue

            if ch == "\r":
                if i == n - 1:
                    # could be the start of a split "\r\n" -- hold it back
                    # and wait for the next chunk to decide
                    break
                flush_plain(i)
                i += 1
                plain_start = i
                if buf[i] == "\n":
                    i += 1
                    plain_start = i
                completed.append(self._current)
                self._current = StyledLine()
                continue

            if ch == "\n":
                flush_plain(i)
                i += 1
                plain_start = i
                completed.append(self._current)
                self._current = StyledLine()
                continue

            i += 1

        flush_plain(i)
        self._buffer = buf[plain_start:] if plain_start < n else ""
        # if we stopped early because of a possibly-truncated escape, keep
        # only the unconsumed tail for next time
        if i < n:
            self._buffer = buf[i:]
        return completed

    def current_line(self) -> StyledLine:
        return self._current

    # -- SGR / CSI handling --------------------------------------------

    def _apply_csi(self, params: str, final: str) -> None:
        if final != "m":
            # cursor movement, clear-screen, etc. -- intentionally ignored
            return
        if params == "":
            self._style = DEFAULT_STYLE
            return
        codes = [int(p) if p else 0 for p in params.split(";")]
        style = self._style
        idx = 0
        while idx < len(codes):
            code = codes[idx]
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
            elif code == 5 or code == 6:
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
            elif code == 38:
                rgb, idx = self._parse_extended_color(codes, idx)
                style = style.merged(fg=rgb)
                idx += 1
                continue
            elif code == 39:
                style = style.merged(fg=None)
            elif 40 <= code <= 47:
                style = style.merged(bg=_BASE16[code - 40])
            elif code == 48:
                rgb, idx = self._parse_extended_color(codes, idx)
                style = style.merged(bg=rgb)
                idx += 1
                continue
            elif code == 49:
                style = style.merged(bg=None)
            elif 90 <= code <= 97:
                style = style.merged(fg=_BASE16[8 + (code - 90)])
            elif 100 <= code <= 107:
                style = style.merged(bg=_BASE16[8 + (code - 100)])
            idx += 1
        self._style = style

    @staticmethod
    def _parse_extended_color(codes: list[int], idx: int) -> tuple[Optional[tuple[int, int, int]], int]:
        """codes[idx] is 38 or 48. Consumes the ';5;n' or ';2;r;g;b' that
        follows and returns (rgb, new_idx) pointing at the last consumed code."""
        if idx + 1 >= len(codes):
            return None, idx
        mode = codes[idx + 1]
        if mode == 5 and idx + 2 < len(codes):
            n = codes[idx + 2]
            palette = _xterm256()
            rgb = palette[n] if 0 <= n < len(palette) else None
            return rgb, idx + 2
        if mode == 2 and idx + 4 < len(codes):
            r, g, b = codes[idx + 2], codes[idx + 3], codes[idx + 4]
            return (r, g, b), idx + 4
        return None, idx + 1


class Scrollback:
    """Bounded ring buffer of StyledLine, with a simple substring search."""

    def __init__(self, max_lines: int = 10_000) -> None:
        self.max_lines = max_lines
        self._lines: list[StyledLine] = []

    def append(self, line: StyledLine) -> None:
        self._lines.append(line)
        if len(self._lines) > self.max_lines:
            del self._lines[: len(self._lines) - self.max_lines]

    def extend(self, lines: list[StyledLine]) -> None:
        for line in lines:
            self.append(line)

    def __len__(self) -> int:
        return len(self._lines)

    def __getitem__(self, idx):
        return self._lines[idx]

    def search(self, needle: str, case_sensitive: bool = False) -> list[int]:
        """Returns indices of lines whose plain text contains needle."""
        if not case_sensitive:
            needle = needle.lower()
        hits = []
        for i, line in enumerate(self._lines):
            text = line.plain_text()
            if not case_sensitive:
                text = text.lower()
            if needle in text:
                hits.append(i)
        return hits

    def tail(self, n: int) -> list[StyledLine]:
        return self._lines[-n:]
