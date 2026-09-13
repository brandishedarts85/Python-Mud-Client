"""
app.py -- the minimal UI shell.

A Textual TUI that wires client_core (networking) -> ansi_parser
(rendering) -> automation (triggers/aliases/timers) into something you
can actually log into a MUD with. This is intentionally thin: its job
is to prove the three layers work together under a real event loop and
give you a usable client today, not to be the final UI.

Usage:
    python3 app.py                      # opens a connect screen
    python3 app.py mud.example.com 4000 # connects immediately

Keys:
    Enter           send the current input line
    Up / Down       command history
    Ctrl+P          command palette (built into Textual)
    Ctrl+C          quit
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass

from rich.style import Style as RichStyle
from rich.text import Text as RichText

from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.css.query import NoMatches
from textual.widgets import Footer, Header, Input, RichLog, Static

from ansi_parser import AnsiParser, Scrollback, Segment, Style, StyledLine
from automation import AutomationEngine
from client_core import EventBus, EventType, MudConnection


def style_to_rich(style: Style) -> RichStyle:
    """Bridges our UI-agnostic Style dataclass to Rich's Style, since
    RichLog (and Textual generally) renders Rich renderables."""
    color = _rgb_to_rich(style.fg)
    bgcolor = _rgb_to_rich(style.bg)
    return RichStyle(
        color=color,
        bgcolor=bgcolor,
        bold=style.bold or None,
        dim=style.dim or None,
        italic=style.italic or None,
        underline=style.underline or None,
        blink=style.blink or None,
        reverse=style.reverse or None,
        strike=style.strike or None,
    )


def _rgb_to_rich(rgb) -> str | None:
    if rgb is None:
        return None
    r, g, b = rgb
    return f"rgb({r},{g},{b})"


def line_to_rich_text(line: StyledLine) -> RichText:
    text = RichText()
    for seg in line.segments:
        text.append(seg.text, style=style_to_rich(seg.style))
    return text


@dataclass
class ConnectionInfo:
    host: str
    port: int


class StatusBar(Static):
    """A one-line status strip: connection state + latest GMCP vitals,
    when the server sends any."""

    status_text: reactive[str] = reactive("disconnected")

    def render(self) -> RichText:
        return RichText(self.status_text)


class MudClientApp(App):
    CSS = """
    Screen {
        layout: vertical;
    }
    #output {
        height: 1fr;
        border: round $primary;
    }
    #statusbar {
        height: 1;
        background: $panel;
        color: $text;
        padding: 0 1;
    }
    #inputbar {
        height: 3;
    }
    """

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
    ]

    def __init__(self, host: str | None = None, port: int | None = None) -> None:
        super().__init__()
        self.host = host
        self.port = port

        self.bus = EventBus()
        self.ansi = AnsiParser()
        self.scrollback = Scrollback(max_lines=10_000)
        self.conn: MudConnection | None = None
        self.engine: AutomationEngine | None = None

        self._history: list[str] = []
        self._history_pos = 0

        self._tick_task: asyncio.Task | None = None

    # -- composition ------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield RichLog(id="output", wrap=True, highlight=False, markup=False)
            yield Static(id="statusbar")
            yield Input(placeholder="type a command and press enter", id="inputbar")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#inputbar", Input).focus()
        self._set_status("disconnected")
        if self.host and self.port:
            await self.start_connection(self.host, self.port)

    # -- connection lifecycle ----------------------------------------------

    async def start_connection(self, host: str, port: int) -> None:
        self.conn = MudConnection(host, port, self.bus, terminal_type="PyMudClient")
        self.engine = AutomationEngine(send_fn=self.conn.send_line)
        self._wire_bus()

        self._set_status(f"connecting to {host}:{port}...")
        try:
            await self.conn.connect()
        except OSError as exc:
            self._write_system_line(f"[connection failed: {exc}]")
            self._set_status("disconnected")
            return

        if self._tick_task is None:
            self._tick_task = asyncio.create_task(self._tick_loop())

    def _wire_bus(self) -> None:
        self.bus.on(EventType.TEXT, lambda e: self._on_incoming(e.data))
        self.bus.on(EventType.PROMPT, lambda e: self._on_incoming(e.data))
        self.bus.on(EventType.CONNECTED, lambda e: self._on_connected(e.data))
        self.bus.on(EventType.DISCONNECTED, lambda e: self._on_disconnected())
        self.bus.on(EventType.GMCP, lambda e: self._on_gmcp(e.data))
        self.bus.on(EventType.ERROR, lambda e: self._write_system_line(f"[error: {e.data}]"))

    def _on_connected(self, data) -> None:
        self._set_status(f"connected to {data['host']}:{data['port']}")

    def _on_disconnected(self) -> None:
        self._set_status("disconnected")
        self._write_system_line("[disconnected]")

    def _on_gmcp(self, data) -> None:
        if data.get("package") == "Char.Vitals" and isinstance(data.get("data"), dict):
            vitals = data["data"]
            hp = vitals.get("hp")
            maxhp = vitals.get("maxhp")
            if hp is not None and maxhp is not None:
                self._set_status(f"connected  |  HP {hp}/{maxhp}")

    def _on_incoming(self, raw_text: str) -> None:
        for line in self.ansi.feed(raw_text):
            self.scrollback.append(line)
            plain = line.plain_text()
            kept = self.engine.on_text(plain) if self.engine else plain
            if kept is not None:
                try:
                    self.query_one("#output", RichLog).write(line_to_rich_text(line))
                except NoMatches:
                    pass  # app is shutting down; nothing to render into

    def _write_system_line(self, text: str) -> None:
        try:
            self.query_one("#output", RichLog).write(RichText(text, style="italic yellow"))
        except NoMatches:
            pass

    def _set_status(self, text: str) -> None:
        try:
            self.query_one("#statusbar", Static).update(text)
        except NoMatches:
            pass  # app is shutting down; no widget left to update

    async def _tick_loop(self) -> None:
        while True:
            if self.engine:
                self.engine.tick()
            await asyncio.sleep(0.1)

    # -- input handling ------------------------------------------------

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        command = event.value
        event.input.value = ""
        if not command:
            return
        self._history.append(command)
        self._history_pos = len(self._history)

        if self.conn is None or self.engine is None:
            self._write_system_line("[not connected]")
            return

        for cmd in self.engine.on_command(command):
            self.conn.send_line(cmd)

    async def on_key(self, event: events.Key) -> None:
        input_widget = self.query_one("#inputbar", Input)
        if not input_widget.has_focus:
            return
        if event.key == "up" and self._history:
            self._history_pos = max(0, self._history_pos - 1)
            input_widget.value = self._history[self._history_pos]
            input_widget.cursor_position = len(input_widget.value)
            event.stop()
        elif event.key == "down" and self._history:
            self._history_pos = min(len(self._history), self._history_pos + 1)
            input_widget.value = (
                self._history[self._history_pos] if self._history_pos < len(self._history) else ""
            )
            input_widget.cursor_position = len(input_widget.value)
            event.stop()

    async def on_unmount(self) -> None:
        if self._tick_task:
            self._tick_task.cancel()
        if self.conn:
            await self.conn.disconnect()


def main() -> None:
    host = sys.argv[1] if len(sys.argv) > 1 else None
    port = int(sys.argv[2]) if len(sys.argv) > 2 else None
    app = MudClientApp(host=host, port=port)
    app.run()


if __name__ == "__main__":
    main()
