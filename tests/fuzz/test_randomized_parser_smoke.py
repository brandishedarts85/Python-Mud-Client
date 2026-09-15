"""Deterministic randomized smoke tests for parser state-machine robustness.

These are not a replacement for a coverage-guided fuzzer.  They are a cheap
release gate that repeatedly changes feed boundaries and hostile byte/text
shapes so state carried across calls gets exercised on every full test run.
"""

import random

from ansi_parser import AnsiParseLimitError, AnsiParser
from client_core import EventBus, MudConnection, TelnetProtocolError


class _Writer:
    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(bytes(data))

    def is_closing(self):
        return False


def test_randomized_telnet_split_smoke():
    rng = random.Random(0xA11CE)

    for _case in range(1000):
        conn = MudConnection("fuzz.invalid", 4000, EventBus())
        conn._writer = _Writer()
        payload = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 384)))

        index = 0
        while index < len(payload):
            width = rng.randrange(1, 24)
            conn._raw_queue.extend(payload[index:index + width])
            index += width
            try:
                conn._drain_queue()
            except TelnetProtocolError:
                # Rejecting malformed/abusive remote input is expected.  The
                # smoke invariant is that parser state never wedges or escapes
                # with an unrelated exception.
                break


def test_randomized_ansi_split_smoke():
    rng = random.Random(0xBADC0DE)
    alphabet = (
        "abcdefghijklmnopqrstuvwxyz0123456789 \r\n"
        "\x1b[];:?=>\\\x07"
    )

    for _case in range(1000):
        parser = AnsiParser(max_line_chars=4096)
        text = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 512)))

        index = 0
        while index < len(text):
            width = rng.randrange(1, 31)
            try:
                parser.feed(text[index:index + width])
            except AnsiParseLimitError:
                parser.reset()
            index += width
