"""
example_wire.py -- minimal end-to-end wiring example.

Shows how the core layers fit together without introducing a UI toolkit:

    client_core
        -> ansi_parser
        -> automation
        -> terminal output

This is intentionally small. A real Textual, Qt, or web UI follows the
same ownership boundaries but renders StyledLine objects instead of
printing their plain text.

Usage:
    python3 example_wire.py mud.example.com 4000
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

from ansi_parser import AnsiParser, StyledLine
from automation import AutomationEngine
from client_core import EventBus, EventType, MudConnection


async def main(host: str, port: int) -> None:
    bus = EventBus()
    ansi = AnsiParser()

    conn = MudConnection(
        host,
        port,
        bus,
        terminal_type="PyMudClient",
    )

    engine = AutomationEngine(
        send_fn=conn.send_line,
    )

    # ------------------------------------------------------------------
    # Example automation
    # ------------------------------------------------------------------

    engine.add_alias(
        "k *",
        "kill %1",
    )

    engine.add_trigger(
        r"You are hungry",
        lambda match, ctx: ctx.send("eat bread"),
        cooldown_s=30,
    )

    # ------------------------------------------------------------------
    # Incoming line processing
    # ------------------------------------------------------------------

    def process_line(line: StyledLine) -> None:
        """
        One semantic line:

            StyledLine
                -> plain text for automation
                -> gag decision
                -> presentation

        A real UI would render `line` with its styles rather than printing
        `plain`.
        """

        plain = line.plain_text()

        kept = engine.on_text(plain)

        if kept is not None:
            print(plain)

    def handle_text(raw_text: str) -> None:
        """
        TEXT events are arbitrary transport chunks.

        Only complete newline-terminated lines returned by AnsiParser are
        passed to automation.
        """

        for line in ansi.feed(raw_text):
            process_line(line)

    def handle_prompt(raw_text: str) -> None:
        """
        GA/EOR establishes an authoritative prompt boundary.

        Feed the prompt bytes first, then explicitly flush the parser's
        unterminated current line.
        """

        for line in ansi.feed(raw_text):
            process_line(line)

        prompt = ansi.flush_line()

        if prompt is not None:
            process_line(prompt)

    def handle_disconnect() -> None:
        """
        Preserve any final visible unterminated text before shutdown.
        """

        final_line = ansi.flush_line()

        if final_line is not None:
            process_line(final_line)

        print("[disconnected]")

    # ------------------------------------------------------------------
    # Event wiring
    # ------------------------------------------------------------------

    bus.on(
        EventType.TEXT,
        lambda event: handle_text(event.data),
    )

    bus.on(
        EventType.PROMPT,
        lambda event: handle_prompt(event.data),
    )

    bus.on(
        EventType.CONNECTED,
        lambda event: print(
            f"[connected to "
            f"{event.data['host']}:{event.data['port']}]"
        ),
    )

    bus.on(
        EventType.DISCONNECTED,
        lambda event: handle_disconnect(),
    )

    bus.on(
        EventType.GMCP,
        lambda event: print(
            f"[GMCP {event.data['package']}] "
            f"{event.data['data']}"
        ),
    )

    bus.on(
        EventType.MSDP,
        lambda event: print(
            f"[MSDP {event.data['variable']}] "
            f"{event.data['value']}"
        ),
    )

    bus.on(
        EventType.ERROR,
        lambda event: print(
            f"[error: {event.data}]"
        ),
    )

    # ------------------------------------------------------------------
    # Connect
    # ------------------------------------------------------------------

    await conn.connect()

    # ------------------------------------------------------------------
    # Periodic automation timer driver
    # ------------------------------------------------------------------

    async def ticker() -> None:
        try:
            while True:
                engine.tick()
                await asyncio.sleep(0.1)

        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------
    # stdin -> aliases -> network
    # ------------------------------------------------------------------

    async def stdin_to_server() -> None:
        while True:
            line = await asyncio.to_thread(
                sys.stdin.readline
            )

            if not line:
                return

            # stdin supplies a trailing newline. Strip only line endings rather
            # than arbitrary whitespace so user-entered spacing is preserved.
            command = line.rstrip("\r\n")

            for expanded in engine.on_command(command):
                conn.send_line(expanded)

    ticker_task = asyncio.create_task(
        ticker(),
        name="example-wire-ticker",
    )

    stdin_task = asyncio.create_task(
        stdin_to_server(),
        name="example-wire-stdin",
    )

    connection_task = asyncio.create_task(
        conn.wait_closed(),
        name="example-wire-connection",
    )

    try:
        # Exit when either:
        #
        #   - the MUD disconnects, or
        #   - stdin closes.
        #
        # The original example used gather(), which could remain blocked on
        # stdin after the network connection had already died.
        done, pending = await asyncio.wait(
            {
                stdin_task,
                connection_task,
            },
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Surface unexpected task errors.
        for task in done:
            with contextlib.suppress(
                asyncio.CancelledError
            ):
                task.result()

    finally:
        ticker_task.cancel()

        if not stdin_task.done():
            stdin_task.cancel()

        if not connection_task.done():
            connection_task.cancel()

        with contextlib.suppress(Exception):
            await conn.disconnect()

        await asyncio.gather(
            ticker_task,
            stdin_task,
            connection_task,
            return_exceptions=True,
        )


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(
            "usage: python3 example_wire.py <host> <port>"
        )
        sys.exit(1)

    try:
        port = int(sys.argv[2])

    except ValueError:
        print(
            f"invalid port: {sys.argv[2]!r}"
        )
        sys.exit(1)

    if not 1 <= port <= 65535:
        print(
            "port must be between 1 and 65535"
        )
        sys.exit(1)

    try:
        asyncio.run(
            main(
                sys.argv[1],
                port,
            )
        )

    except KeyboardInterrupt:
        pass
