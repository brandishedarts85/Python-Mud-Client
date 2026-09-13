"""
example_wire.py -- shows how the three layers plug together.

This is not a UI. It's the ~30 lines that any real UI (Qt/Textual/web)
would write once, at startup, to connect networking -> rendering ->
automation. Run it against a real MUD to see it work end to end:

    python3 example_wire.py mud.example.com 4000
"""

import asyncio
import sys

from client_core import MudConnection, EventBus, EventType
from ansi_parser import AnsiParser
from automation import AutomationEngine


async def main(host: str, port: int) -> None:
    bus = EventBus()
    ansi = AnsiParser()

    conn = MudConnection(host, port, bus, terminal_type="PyMudClient")
    engine = AutomationEngine(send_fn=conn.send_line)

    # example automation, just to prove the wiring works
    engine.add_alias("k *", "kill %1")
    engine.add_trigger(
        r"You are hungry",
        lambda m, ctx: ctx.send("eat bread"),
        cooldown_s=30,
    )

    def handle_incoming(raw_text: str) -> None:
        for line in ansi.feed(raw_text):
            plain = line.plain_text()
            kept = engine.on_text(plain)
            if kept is not None:
                print(kept)  # a real UI would render `line` (with styles), not `kept`

    bus.on(EventType.TEXT, lambda e: handle_incoming(e.data))
    bus.on(EventType.PROMPT, lambda e: handle_incoming(e.data))
    bus.on(EventType.CONNECTED, lambda e: print(f"[connected to {host}:{port}]"))
    bus.on(EventType.DISCONNECTED, lambda e: print("[disconnected]"))
    bus.on(EventType.GMCP, lambda e: print(f"[GMCP {e.data['package']}] {e.data['data']}"))

    await conn.connect()

    async def ticker():
        while True:
            engine.tick()
            await asyncio.sleep(0.1)

    tick_task = asyncio.create_task(ticker())

    async def stdin_to_server():
        loop = asyncio.get_event_loop()
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            for cmd in engine.on_command(line.rstrip("\n")):
                conn.send_line(cmd)

    await asyncio.gather(conn.wait_closed(), stdin_to_server())
    tick_task.cancel()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: python3 example_wire.py <host> <port>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1], int(sys.argv[2])))
