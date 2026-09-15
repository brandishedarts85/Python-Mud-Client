"""
app.py -- the minimal UI shell.

A Textual TUI that wires client_core (networking) -> ansi_parser
(rendering) -> automation (triggers/aliases/timers) into something you
can actually log into a MUD with.

This module intentionally remains thin. Networking semantics belong in
client_core, terminal styling belongs in ansi_parser, automation belongs
in automation, and this module owns UI/event-loop integration.

Usage:
    python3 app.py
    python3 app.py mud.example.com 4000
    python3 app.py despair

Keys:
    Enter           send the current input line
    Up / Down       command history
    Ctrl+P          command palette (built into Textual)
    Ctrl+C          quit

In-app commands:
    #alias PATTERN = EXPANSION
    #unalias PATTERN
    #trigger PATTERN = RESPONSE [:: gag] [oneshot] [cooldown=N]
    #untrigger PATTERN
    #list
    #save
    #saveprofile NAME
    #reconnect
    #disconnect
    #help

Connection hardening:
  - unexpected disconnects automatically reconnect with exponential backoff
  - each transport receives its own EventBus generation
  - events from retired connections are ignored
  - aliases/triggers survive transport reconnects even when not yet saved
  - GA/EOR remains the authoritative prompt boundary
  - a short idle fallback handles older servers that send prompts without
    GA/EOR; this fallback is only a display heuristic, not protocol truth
"""

from __future__ import annotations

import asyncio
import sys
import time
from contextlib import suppress

from rich.style import Style as RichStyle
from rich.text import Text as RichText

from textual import events
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.widgets import Footer, Header, Input, RichLog, Static

from ansi_parser import AnsiParser, Scrollback, Style, StyledLine
from automation import AutomationEngine
from client_core import EventBus, EventType, MudConnection
from persistence import (
    DEFAULT_AUTOMATION_PATH,
    load_automation,
    resolve_connection,
    save_automation,
    save_profile,
)


# ---------------------------------------------------------------------------
# Rendering bridge
# ---------------------------------------------------------------------------


def style_to_rich(style: Style) -> RichStyle:
    """
    Convert the UI-agnostic ANSI Style object into a Rich Style.

    ansi_parser intentionally has no Rich/Textual dependency; this is the
    presentation boundary between the parser and this UI.
    """
    return RichStyle(
        color=_rgb_to_rich(style.fg),
        bgcolor=_rgb_to_rich(style.bg),
        bold=style.bold or None,
        dim=style.dim or None,
        italic=style.italic or None,
        underline=style.underline or None,
        blink=style.blink or None,
        reverse=style.reverse or None,
        strike=style.strike or None,
    )


def _rgb_to_rich(rgb: tuple[int, int, int] | None) -> str | None:
    if rgb is None:
        return None

    r, g, b = rgb
    return f"rgb({r},{g},{b})"


def line_to_rich_text(line: StyledLine) -> RichText:
    text = RichText()

    for segment in line.segments:
        text.append(
            segment.text,
            style=style_to_rich(segment.style),
        )

    return text


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------


class StatusBar(Static):
    """Connection state and lightweight structured status information."""

    status_text: reactive[str] = reactive("disconnected")

    def render(self) -> RichText:
        return RichText(self.status_text)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


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

    # Tests can override these class attributes.
    RECONNECT_BASE_DELAY = 3.0
    RECONNECT_MAX_DELAY = 60.0

    # Compatibility fallback for servers that do not terminate prompts with
    # GA or EOR. This is deliberately treated as a heuristic display boundary.
    PROMPT_IDLE_TIMEOUT = 0.3

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
    ) -> None:
        super().__init__()

        self.host = host
        self.port = port

        # One parser represents only the currently active transport stream.
        self.ansi = AnsiParser()

        # Scrollback intentionally survives reconnects.
        self.scrollback = Scrollback(max_lines=10_000)

        # A new EventBus is created for every transport generation.
        # Keeping the attribute is useful for diagnostics/tests.
        self.bus = EventBus()

        self.conn: MudConnection | None = None

        # ------------------------------------------------------------------
        # Automation ownership
        #
        # Automation is session/application state, not transport state.
        #
        # We therefore construct it exactly once. Its send callback dynamically
        # targets self.conn, allowing reconnects to replace MudConnection
        # without destroying unsaved aliases/triggers.
        # ------------------------------------------------------------------

        self.engine = AutomationEngine(send_fn=self._send_current_line)

        (
            self._loaded_alias_count,
            self._loaded_trigger_count,
        ) = load_automation(
            self.engine,
            DEFAULT_AUTOMATION_PATH,
        )

        self._automation_load_reported = False

        # Command history.
        self._history: list[str] = []
        self._history_pos = 0

        # Main periodic task.
        self._tick_task: asyncio.Task | None = None

        # Last arrival time for idle-prompt compatibility fallback.
        self._last_text_time = 0.0

        # Reconnect state.
        self._manual_disconnect = False
        self._shutting_down = False

        self._reconnect_task: asyncio.Task | None = None
        self._reconnect_attempt = 0

        # ------------------------------------------------------------------
        # Transport generation
        #
        # Every MudConnection receives a generation-bound EventBus.
        #
        # If an old connection finishes shutting down after a replacement is
        # already active, its late DISCONNECTED/TEXT/ERROR events are ignored.
        # ------------------------------------------------------------------

        self._connection_generation = 0

    # ------------------------------------------------------------------
    # Composition
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        with Vertical():
            yield RichLog(
                id="output",
                wrap=True,
                highlight=False,
                markup=False,
            )

            yield StatusBar(id="statusbar")

            yield Input(
                placeholder="type a command and press enter",
                id="inputbar",
            )

        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#inputbar", Input).focus()

        self._set_status("disconnected")

        if self.host and self.port:
            await self.start_connection(
                self.host,
                self.port,
            )

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def start_connection(
        self,
        host: str,
        port: int,
    ) -> None:
        """
        Create a new transport generation.

        The connection, EventBus, and ANSI parser belong to this transport.
        Scrollback and automation belong to the application and survive it.
        """

        self.host = host
        self.port = port

        self._connection_generation += 1
        generation = self._connection_generation

        # A transport stream may end halfway through an ANSI sequence or with
        # active style state. Never allow either to bleed into a replacement
        # connection.
        self.ansi = AnsiParser()

        bus = EventBus()
        self.bus = bus

        self._wire_bus(
            bus,
            generation,
        )

        conn = MudConnection(
            host,
            port,
            bus,
            terminal_type="PyMudClient",
        )

        self.conn = conn

        self._set_status(
            f"connecting to {host}:{port}..."
        )

        try:
            await conn.connect()

        except OSError as exc:
            # Only report/schedule if this attempt has not already been
            # superseded by another transport generation.
            if self._is_current_generation(generation):
                self._write_system_line(
                    f"[connection failed: {exc}]"
                )
                self._set_status("disconnected")
                self._schedule_reconnect()

            return

        # A newer connection may theoretically have superseded this one while
        # connect() was awaiting.
        if not self._is_current_generation(generation):
            with suppress(Exception):
                await conn.disconnect()
            return

        self._reconnect_attempt = 0

        if not self._automation_load_reported:
            if self._loaded_alias_count or self._loaded_trigger_count:
                self._write_system_line(
                    f"[loaded {self._loaded_alias_count} alias(es) and "
                    f"{self._loaded_trigger_count} trigger(s) from "
                    f"{DEFAULT_AUTOMATION_PATH}]"
                )

            self._automation_load_reported = True

        if self._tick_task is None or self._tick_task.done():
            self._tick_task = asyncio.create_task(
                self._tick_loop(),
                name="mud-client-tick-loop",
            )

    def _wire_bus(
        self,
        bus: EventBus,
        generation: int,
    ) -> None:
        """
        Wire handlers to one specific transport generation.

        Capturing generation here prevents retired transports from mutating the
        UI/reconnect state after a replacement connection exists.
        """

        bus.on(
            EventType.TEXT,
            lambda event: self._on_incoming(
                generation,
                event.data,
                is_prompt=False,
            ),
        )

        bus.on(
            EventType.PROMPT,
            lambda event: self._on_incoming(
                generation,
                event.data,
                is_prompt=True,
            ),
        )

        bus.on(
            EventType.CONNECTED,
            lambda event: self._on_connected(
                generation,
                event.data,
            ),
        )

        bus.on(
            EventType.DISCONNECTED,
            lambda event: self._on_disconnected(
                generation,
            ),
        )

        bus.on(
            EventType.GMCP,
            lambda event: self._on_gmcp(
                generation,
                event.data,
            ),
        )

        bus.on(
            EventType.ERROR,
            lambda event: self._on_error(
                generation,
                event.data,
            ),
        )

    def _is_current_generation(
        self,
        generation: int,
    ) -> bool:
        return generation == self._connection_generation

    def _on_connected(
        self,
        generation: int,
        data,
    ) -> None:
        if not self._is_current_generation(generation):
            return

        self._set_status(
            f"connected to {data['host']}:{data['port']}"
        )

    def _on_disconnected(
        self,
        generation: int,
    ) -> None:
        if not self._is_current_generation(generation):
            return

        # Preserve any final visible unterminated text from the dead transport
        # before the next connection gets a clean ANSI parser.
        flushed = self.ansi.flush_line()

        if flushed is not None:
            self._process_line(flushed)

        self._last_text_time = 0.0

        self._set_status("disconnected")
        self._write_system_line("[disconnected]")

        self._schedule_reconnect()

    def _on_error(
        self,
        generation: int,
        data,
    ) -> None:
        if not self._is_current_generation(generation):
            return

        self._write_system_line(
            f"[error: {data}]"
        )

    def _schedule_reconnect(self) -> None:
        if self._manual_disconnect:
            return

        if self._shutting_down:
            return

        if (
            self._reconnect_task is not None
            and not self._reconnect_task.done()
        ):
            return

        delay = min(
            self.RECONNECT_BASE_DELAY
            * (2 ** self._reconnect_attempt),
            self.RECONNECT_MAX_DELAY,
        )

        self._reconnect_attempt += 1

        self._write_system_line(
            f"[reconnecting in {delay:.0f}s...]"
        )

        self._reconnect_task = asyncio.create_task(
            self._reconnect_after(delay),
            name="mud-client-reconnect",
        )

    async def _reconnect_after(
        self,
        delay: float,
    ) -> None:
        try:
            await asyncio.sleep(delay)

            if self._manual_disconnect:
                return

            if self._shutting_down:
                return

            if not self.host or self.port is None:
                return

            self._write_system_line(
                f"[reconnecting to {self.host}:{self.port}...]"
            )

            await self.start_connection(
                self.host,
                self.port,
            )

        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------
    # Outbound transport adapter
    # ------------------------------------------------------------------

    def _send_current_line(
        self,
        line: str,
    ) -> None:
        """
        Stable AutomationEngine send target.

        Automation survives reconnects; this adapter always routes an action to
        the currently active MudConnection instead of binding the engine to one
        retired connection object's send_line method.
        """

        conn = self.conn

        if conn is None:
            return

        conn.send_line(line)

    # ------------------------------------------------------------------
    # Incoming text / rendering
    # ------------------------------------------------------------------

    def _on_incoming(
        self,
        generation: int,
        raw_text: str,
        is_prompt: bool = False,
    ) -> None:
        if not self._is_current_generation(generation):
            return

        self._last_text_time = time.monotonic()

        lines = self.ansi.feed(raw_text)

        for line in lines:
            self._process_line(line)

        if is_prompt:
            # GA/EOR is an authoritative protocol-level prompt boundary.
            flushed = self.ansi.flush_line()

            if flushed is not None:
                self._process_line(flushed)

            # Do not let the heuristic idle timer treat the already completed
            # protocol prompt as another candidate.
            self._last_text_time = 0.0

    def _process_line(
        self,
        line: StyledLine,
    ) -> None:
        """
        Single path for all completed display lines.

        Ordering deliberately remains:

            scrollback
                -> plain-text automation
                -> gag decision
                -> styled rendering

        Gagged text therefore remains available in scrollback/history while
        disappearing from the visible RichLog, matching the original behavior.
        """

        self.scrollback.append(line)

        plain = line.plain_text()

        kept = self.engine.on_text(plain)

        if kept is None:
            return

        try:
            output = self.query_one(
                "#output",
                RichLog,
            )

            output.write(
                line_to_rich_text(line)
            )

        except NoMatches:
            # UI teardown may race a late event.
            pass

    def _check_prompt_idle_timeout(self) -> None:
        """
        Compatibility fallback for MUDs that emit prompts without GA/EOR.

        IMPORTANT:
        This is not protocol-level prompt detection.

        It merely says:
            "the server has left visible unterminated text idle long enough
             that displaying it now is preferable to leaving it invisible."

        GA/EOR remains authoritative when available.
        """

        if self._last_text_time == 0.0:
            return

        idle_for = (
            time.monotonic()
            - self._last_text_time
        )

        if idle_for < self.PROMPT_IDLE_TIMEOUT:
            return

        flushed = self.ansi.flush_line()

        # Whether or not anything flushed, do not repeatedly test the same
        # idle state every 100 ms. New incoming text arms the timer again.
        self._last_text_time = 0.0

        if flushed is None:
            return

        self._process_line(flushed)

    # ------------------------------------------------------------------
    # Structured protocol data
    # ------------------------------------------------------------------

    def _on_gmcp(
        self,
        generation: int,
        data,
    ) -> None:
        if not self._is_current_generation(generation):
            return

        if data.get("package") != "Char.Vitals":
            return

        payload = data.get("data")

        if not isinstance(payload, dict):
            return

        hp = payload.get("hp")
        maxhp = payload.get("maxhp")

        if hp is None or maxhp is None:
            return

        self._set_status(
            f"connected  |  HP {hp}/{maxhp}"
        )

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------

    def _write_system_line(
        self,
        text: str,
    ) -> None:
        try:
            self.query_one(
                "#output",
                RichLog,
            ).write(
                RichText(
                    text,
                    style="italic yellow",
                )
            )

        except NoMatches:
            pass

    def _set_status(
        self,
        text: str,
    ) -> None:
        try:
            self.query_one(
                "#statusbar",
                StatusBar,
            ).status_text = text

        except NoMatches:
            pass

    # ------------------------------------------------------------------
    # Tick loop
    # ------------------------------------------------------------------

    async def _tick_loop(self) -> None:
        try:
            while True:
                self.engine.tick()

                self._check_prompt_idle_timeout()

                await asyncio.sleep(0.1)

        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------

    async def on_input_submitted(
        self,
        event: Input.Submitted,
    ) -> None:
        command = event.value
        event.input.value = ""

        if command.startswith("#"):
            self._handle_slash_command(
                command[1:].strip()
            )
            return

        if self.conn is None:
            self._write_system_line(
                "[not connected]"
            )
            return

        if command:
            self._history.append(command)
            self._history_pos = len(self._history)

            for expanded_command in self.engine.on_command(command):
                self.conn.send_line(
                    expanded_command
                )

        else:
            # Blank Enter is meaningful to many MUDs.
            self.conn.send_line("")

    # ------------------------------------------------------------------
    # Client-side commands
    # ------------------------------------------------------------------

    def _handle_slash_command(
        self,
        body: str,
    ) -> None:
        parts = body.split(None, 1)

        cmd = (
            parts[0].lower()
            if parts
            else ""
        )

        rest = (
            parts[1]
            if len(parts) > 1
            else ""
        )

        if cmd == "alias":
            if " = " not in rest:
                self._write_system_line(
                    "[usage: #alias PATTERN = EXPANSION]"
                )
                return

            pattern, expansion = rest.split(
                " = ",
                1,
            )

            pattern = pattern.strip()
            expansion = expansion.strip()

            self.engine.add_alias(
                pattern,
                expansion,
            )

            self._write_system_line(
                f"[alias added: {pattern!r} -> {expansion!r}]"
            )

        elif cmd == "unalias":
            pattern = rest.strip()

            removed = self._remove_by_pattern(
                self.engine.aliases,
                pattern,
            )

            self._write_system_line(
                f"[removed {removed} alias(es) matching {pattern!r}]"
            )

        elif cmd == "trigger":
            if " = " not in rest:
                self._write_system_line(
                    "[usage: #trigger PATTERN = RESPONSE "
                    "[:: gag oneshot cooldown=N]]"
                )
                return

            pattern, remainder = rest.split(
                " = ",
                1,
            )

            response = remainder
            flags = ""

            if " :: " in remainder:
                response, flags = remainder.split(
                    " :: ",
                    1,
                )

            flag_tokens = flags.split()

            gag = "gag" in flag_tokens
            one_shot = "oneshot" in flag_tokens

            cooldown_s = 0.0

            for token in flag_tokens:
                if not token.startswith("cooldown="):
                    continue

                try:
                    cooldown_s = float(
                        token.split(
                            "=",
                            1,
                        )[1]
                    )
                except ValueError:
                    self._write_system_line(
                        f"[invalid cooldown value: {token!r}]"
                    )
                    return

            pattern = pattern.strip()
            response = response.strip()

            self.engine.add_simple_trigger(
                pattern,
                response,
                gag=gag,
                one_shot=one_shot,
                cooldown_s=cooldown_s,
            )

            suffixes: list[str] = []

            if gag:
                suffixes.append("gag")

            if one_shot:
                suffixes.append("one-shot")

            if cooldown_s:
                suffixes.append(
                    f"cooldown {cooldown_s}s"
                )

            suffix = (
                f" ({', '.join(suffixes)})"
                if suffixes
                else ""
            )

            self._write_system_line(
                f"[trigger added: {pattern!r} -> {response!r}{suffix}]"
            )

        elif cmd == "untrigger":
            pattern = rest.strip()

            removed = self._remove_by_pattern(
                self.engine.triggers,
                pattern,
            )

            self._write_system_line(
                f"[removed {removed} trigger(s) matching {pattern!r}]"
            )

        elif cmd == "list":
            alias_lines = [
                f"  alias:   {alias.pattern!r} -> {alias.expansion!r}"
                for alias in self.engine.aliases.values()
                if isinstance(
                    alias.expansion,
                    str,
                )
            ]

            trigger_lines = [
                f"  trigger: {trigger.pattern!r} -> "
                f"{trigger.response_template!r}"
                for trigger in self.engine.triggers.values()
                if trigger.response_template is not None
            ]

            if not alias_lines and not trigger_lines:
                self._write_system_line(
                    "[no aliases or triggers defined]"
                )
                return

            self._write_system_line(
                "[aliases/triggers]"
            )

            for line in alias_lines + trigger_lines:
                self._write_system_line(line)

        elif cmd == "save":
            save_automation(
                self.engine,
                DEFAULT_AUTOMATION_PATH,
            )

            self._write_system_line(
                f"[saved aliases/triggers to {DEFAULT_AUTOMATION_PATH}]"
            )

        elif cmd == "saveprofile":
            name = rest.strip()

            if not name:
                self._write_system_line(
                    "[usage: #saveprofile NAME]"
                )

            elif self.conn is None:
                self._write_system_line(
                    "[not connected -- nothing to save]"
                )

            else:
                save_profile(
                    name,
                    self.conn.host,
                    self.conn.port,
                )

                self._write_system_line(
                    f"[saved profile {name!r} -> "
                    f"{self.conn.host}:{self.conn.port}]"
                )

        elif cmd == "reconnect":
            self._cancel_reconnect_task()

            self._reconnect_attempt = 0

            if not self.host or self.port is None:
                self._write_system_line(
                    "[no previous connection to reconnect to]"
                )
                return

            self._write_system_line(
                f"[reconnecting to {self.host}:{self.port}...]"
            )

            asyncio.create_task(
                self._manual_reconnect(),
                name="mud-client-manual-reconnect",
            )

        elif cmd == "disconnect":
            self._manual_disconnect = True

            self._cancel_reconnect_task()

            if self.conn is None:
                self._write_system_line(
                    "[not connected]"
                )
                return

            asyncio.create_task(
                self.conn.disconnect(),
                name="mud-client-manual-disconnect",
            )

            self._write_system_line(
                "[disconnected by user -- use #reconnect to reconnect]"
            )

        elif cmd in ("help", ""):
            self._write_system_line(
                "[commands: "
                "#alias P = E | "
                "#unalias P | "
                "#trigger P = R [:: gag oneshot cooldown=N] | "
                "#untrigger P | "
                "#list | "
                "#save | "
                "#saveprofile NAME | "
                "#reconnect | "
                "#disconnect]"
            )

        else:
            self._write_system_line(
                f"[unknown command: #{cmd} -- try #help]"
            )

    async def _manual_reconnect(self) -> None:
        """
        Explicit reconnect without triggering the normal backoff path.
        """

        self._manual_disconnect = True

        old_conn = self.conn

        if old_conn is not None:
            with suppress(Exception):
                await old_conn.disconnect()

        self._manual_disconnect = False

        if self._shutting_down:
            return

        if not self.host or self.port is None:
            return

        await self.start_connection(
            self.host,
            self.port,
        )

    def _cancel_reconnect_task(self) -> None:
        task = self._reconnect_task

        if task is None:
            return

        if not task.done():
            task.cancel()

        self._reconnect_task = None

    @staticmethod
    def _remove_by_pattern(
        store: dict,
        pattern: str,
    ) -> int:
        to_remove = [
            identifier
            for identifier, obj in store.items()
            if obj.pattern == pattern
        ]

        for identifier in to_remove:
            del store[identifier]

        return len(to_remove)

    # ------------------------------------------------------------------
    # Command history
    # ------------------------------------------------------------------

    async def on_key(
        self,
        event: events.Key,
    ) -> None:
        input_widget = self.query_one(
            "#inputbar",
            Input,
        )

        if not input_widget.has_focus:
            return

        if event.key == "up" and self._history:
            self._history_pos = max(
                0,
                self._history_pos - 1,
            )

            input_widget.value = self._history[
                self._history_pos
            ]

            input_widget.cursor_position = len(
                input_widget.value
            )

            event.stop()

        elif event.key == "down" and self._history:
            self._history_pos = min(
                len(self._history),
                self._history_pos + 1,
            )

            if self._history_pos < len(self._history):
                input_widget.value = self._history[
                    self._history_pos
                ]
            else:
                input_widget.value = ""

            input_widget.cursor_position = len(
                input_widget.value
            )

            event.stop()

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def on_unmount(self) -> None:
        self._shutting_down = True
        self._manual_disconnect = True

        tasks: list[asyncio.Task] = []

        if (
            self._reconnect_task is not None
            and not self._reconnect_task.done()
        ):
            self._reconnect_task.cancel()
            tasks.append(self._reconnect_task)

        if (
            self._tick_task is not None
            and not self._tick_task.done()
        ):
            self._tick_task.cancel()
            tasks.append(self._tick_task)

        if self.conn is not None:
            with suppress(Exception):
                await self.conn.disconnect()

        if tasks:
            await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------


def main() -> None:
    arg1 = (
        sys.argv[1]
        if len(sys.argv) > 1
        else None
    )

    arg2 = (
        sys.argv[2]
        if len(sys.argv) > 2
        else None
    )

    host, port = resolve_connection(
        arg1,
        arg2,
    )

    if host and port is None:
        print(
            f"'{host}' isn't a saved profile and no port was given."
        )
        print(
            "Usage: python3 app.py <host> <port>"
        )
        print(
            "   or: python3 app.py <saved-profile-name>"
        )
        sys.exit(1)

    app = MudClientApp(
        host=host,
        port=port,
    )

    app.run()


if __name__ == "__main__":
    main()
