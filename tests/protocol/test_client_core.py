from client_core import EventBus, EventType


def test_event_bus_subscribe_emit_unsubscribe():
    bus = EventBus()
    seen = []

    def handler(event):
        seen.append(event.data)

    bus.on(EventType.TEXT, handler)
    bus.emit(EventType.TEXT, "hello")
    bus.off(EventType.TEXT, handler)
    bus.emit(EventType.TEXT, "ignored")

    assert seen == ["hello"]


class _FakeWriter:
    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(data)

    def is_closing(self):
        return False


def test_ttype_send_advertises_xterm_256color():
    from client_core import IAC, SB, SE, OPT_TTYPE, MudConnection

    bus = EventBus()
    conn = MudConnection("example.org", 4000, bus)
    writer = _FakeWriter()
    conn._writer = writer

    conn._handle_ttype(b"\x01")

    assert writer.writes == [
        bytes([IAC, SB, OPT_TTYPE, 0])
        + b"xterm-256color"
        + bytes([IAC, SE])
    ]


def test_mccp_decompression_is_bounded_before_feed():
    import zlib

    from client_core import (
        MAX_MCCP_OUTPUT_PER_READ,
        MudConnection,
        TelnetProtocolError,
    )

    # Highly compressible input is the classic decompression-bomb shape: a
    # tiny compressed payload that expands just beyond the permitted output.
    compressed = zlib.compress(b"A" * (MAX_MCCP_OUTPUT_PER_READ + 1), level=9)

    conn = MudConnection("example.org", 4000, EventBus())
    conn._mccp_active = True
    conn._decompressor = zlib.decompressobj()
    conn._raw_queue.extend(compressed)

    try:
        conn._drain_queue()
    except TelnetProtocolError as exc:
        assert "decompressed output exceeds" in str(exc)
    else:
        raise AssertionError("oversized MCCP output was not rejected")


def test_mccp_decompress_call_uses_output_limit():
    from client_core import MAX_MCCP_OUTPUT_PER_READ, MudConnection, TelnetProtocolError

    class RecordingDecompressor:
        eof = False
        unused_data = b""

        def __init__(self):
            self.max_length = None

        def decompress(self, data, max_length=0):
            self.max_length = max_length
            return b"X" * max_length

    conn = MudConnection("example.org", 4000, EventBus())
    fake = RecordingDecompressor()
    conn._mccp_active = True
    conn._decompressor = fake
    conn._raw_queue.extend(b"compressed")

    try:
        conn._drain_queue()
    except TelnetProtocolError:
        pass
    else:
        raise AssertionError("sentinel overflow was not rejected")

    assert fake.max_length == MAX_MCCP_OUTPUT_PER_READ + 1


def _activate_mccp_for_test(conn):
    import zlib

    conn._mccp_active = True
    conn._decompressor = zlib.decompressobj()


def test_mccp_clean_zlib_eof_resets_compression_state_and_delivers_text_once():
    import zlib

    from client_core import MudConnection

    bus = EventBus()
    seen = []
    bus.on(EventType.TEXT, lambda event: seen.append(event.data))

    conn = MudConnection("example.org", 4000, bus)
    _activate_mccp_for_test(conn)
    conn._raw_queue.extend(zlib.compress(b"compressed text\n"))

    conn._drain_queue()

    assert seen == ["compressed text\n"]
    assert conn._mccp_active is False
    assert conn._decompressor is None
    assert conn._raw_queue == bytearray()


def test_mccp_eof_trailing_raw_text_is_reparsed_after_compressed_text_in_order():
    import zlib

    from client_core import MudConnection

    bus = EventBus()
    seen = []
    bus.on(EventType.TEXT, lambda event: seen.append(event.data))

    conn = MudConnection("example.org", 4000, bus)
    _activate_mccp_for_test(conn)
    conn._raw_queue.extend(zlib.compress(b"compressed\n") + b"raw-after\n")

    conn._drain_queue()

    assert seen == ["compressed\n", "raw-after\n"]
    assert conn._mccp_active is False
    assert conn._decompressor is None
    assert conn._raw_queue == bytearray()


def test_mccp_eof_trailing_telnet_negotiation_is_not_treated_as_compressed_data():
    import zlib

    from client_core import IAC, WILL, DO, OPT_ECHO, MudConnection

    bus = EventBus()
    option_changes = []
    bus.on(EventType.OPTION_CHANGE, lambda event: option_changes.append(event.data))

    conn = MudConnection("example.org", 4000, bus)
    writer = _FakeWriter()
    conn._writer = writer
    _activate_mccp_for_test(conn)

    conn._raw_queue.extend(
        zlib.compress(b"done\n")
        + bytes([IAC, WILL, OPT_ECHO])
    )

    conn._drain_queue()

    assert conn._mccp_active is False
    assert conn._decompressor is None
    assert option_changes == [{"option": OPT_ECHO, "state": "will"}]
    assert writer.writes[-1] == bytes([IAC, DO, OPT_ECHO])


def test_mccp_raw_bytes_on_following_read_stay_raw_after_clean_eof():
    import zlib

    from client_core import MudConnection

    bus = EventBus()
    seen = []
    bus.on(EventType.TEXT, lambda event: seen.append(event.data))

    conn = MudConnection("example.org", 4000, bus)
    _activate_mccp_for_test(conn)

    conn._raw_queue.extend(zlib.compress(b"compressed\n"))
    conn._drain_queue()
    assert conn._mccp_active is False

    conn._raw_queue.extend(b"next-read-is-raw\n")
    conn._drain_queue()

    assert seen == ["compressed\n", "next-read-is-raw\n"]


def test_mccp_malformed_compressed_stream_raises_protocol_error_without_raw_fallback():
    from client_core import MudConnection, TelnetProtocolError

    bus = EventBus()
    seen = []
    bus.on(EventType.TEXT, lambda event: seen.append(event.data))

    conn = MudConnection("example.org", 4000, bus)
    _activate_mccp_for_test(conn)
    conn._raw_queue.extend(b"this is not a zlib stream")

    try:
        conn._drain_queue()
    except TelnetProtocolError as exc:
        assert "MCCP2 decompression failed" in str(exc)
    else:
        raise AssertionError("malformed MCCP2 stream was not rejected")

    assert seen == []


def test_mccp_eof_trailing_raw_prompt_with_eor_is_parsed_normally():
    import zlib

    from client_core import IAC, EOR_MARK, MudConnection

    bus = EventBus()
    events = []
    bus.on(EventType.TEXT, lambda event: events.append(("text", event.data)))
    bus.on(EventType.PROMPT, lambda event: events.append(("prompt", event.data)))

    conn = MudConnection("example.org", 4000, bus)
    _activate_mccp_for_test(conn)

    # The compressed stream ends cleanly.  Bytes after Z_FINISH are ordinary
    # Telnet again, so a raw prompt + EOR marker must be handled by the normal
    # Telnet parser rather than fed back into zlib.
    conn._raw_queue.extend(
        zlib.compress(b"")
        + b"prompt> "
        + bytes([IAC, EOR_MARK])
    )

    conn._drain_queue()

    # At the transport layer, normal text is emitted as TEXT and EOR is a
    # separate PROMPT boundary.  SessionController intentionally glues these
    # together via AnsiParser buffering.  The key MCCP invariant here is that
    # both raw bytes survive the Z_FINISH transition in the correct order.
    assert events == [("text", "prompt> "), ("prompt", "")]
    assert conn._mccp_active is False
    assert conn._decompressor is None


def test_mccp_activation_compressed_payload_and_post_eof_raw_data_can_share_one_socket_read():
    import zlib

    from client_core import IAC, SB, SE, OPT_MCCP2, MudConnection

    bus = EventBus()
    seen = []
    bus.on(EventType.TEXT, lambda event: seen.append(event.data))

    conn = MudConnection("example.org", 4000, bus)

    wire = (
        bytes([IAC, SB, OPT_MCCP2, IAC, SE])
        + zlib.compress(b"inside-compression\n")
        + b"after-compression\n"
    )
    conn._raw_queue.extend(wire)

    conn._drain_queue()

    assert seen == ["inside-compression\n", "after-compression\n"]
    assert conn._mccp_active is False
    assert conn._decompressor is None
    assert conn._raw_queue == bytearray()


def test_msdp_nesting_limit_rejects_deep_remote_structure():
    from client_core import MAX_MSDP_NESTING, MSDP_ARRAY_OPEN, MSDP_ARRAY_CLOSE, MSDP_VAL, MSDP_VAR, _parse_msdp

    payload = (
        bytes([MSDP_VAR]) + b"DEEP" + bytes([MSDP_VAL])
        + bytes([MSDP_ARRAY_OPEN]) * (MAX_MSDP_NESTING + 2)
        + b"x"
        + bytes([MSDP_ARRAY_CLOSE]) * (MAX_MSDP_NESTING + 2)
    )

    try:
        _parse_msdp(payload)
    except ValueError as exc:
        assert "nesting exceeds" in str(exc)
    else:
        raise AssertionError("deeply nested MSDP was not rejected")


def test_msdp_nesting_at_limit_still_parses():
    from client_core import MAX_MSDP_NESTING, MSDP_ARRAY_OPEN, MSDP_ARRAY_CLOSE, MSDP_VAL, MSDP_VAR, _parse_msdp

    depth = MAX_MSDP_NESTING
    payload = (
        bytes([MSDP_VAR]) + b"OK" + bytes([MSDP_VAL])
        + bytes([MSDP_ARRAY_OPEN]) * depth
        + b"x"
        + bytes([MSDP_ARRAY_CLOSE]) * depth
    )

    parsed = _parse_msdp(payload)
    value = parsed["OK"]
    for _ in range(depth):
        assert isinstance(value, list) and len(value) == 1
        value = value[0]
    assert value == "x"


def test_deep_msdp_payload_is_dropped_without_escaping_handler():
    from client_core import MAX_MSDP_NESTING, MSDP_ARRAY_OPEN, MSDP_ARRAY_CLOSE, MSDP_VAL, MSDP_VAR, MudConnection

    bus = EventBus()
    seen = []
    bus.on(EventType.MSDP, lambda event: seen.append(event.data))
    conn = MudConnection("example.org", 4000, bus)
    payload = (
        bytes([MSDP_VAR]) + b"DEEP" + bytes([MSDP_VAL])
        + bytes([MSDP_ARRAY_OPEN]) * (MAX_MSDP_NESTING + 2)
        + b"x"
        + bytes([MSDP_ARRAY_CLOSE]) * (MAX_MSDP_NESTING + 2)
    )

    # Malformed/pathological MSDP is isolated to that message.  The handler
    # logs and drops it instead of allowing recursion failure to escape into
    # the connection/session layer.
    conn._handle_msdp(payload)

    assert seen == []


def test_gmcp_subnegotiation_is_rejected_at_option_specific_limit():
    from client_core import IAC, SB, OPT_GMCP, MAX_GMCP_BYTES, MudConnection, TelnetProtocolError

    conn = MudConnection("example.org", 4000, EventBus())
    wire = bytes([IAC, SB, OPT_GMCP]) + (b"A" * (MAX_GMCP_BYTES + 1))

    try:
        conn._feed(wire)
    except TelnetProtocolError as exc:
        message = str(exc)
        assert f"option {OPT_GMCP}" in message
        assert str(MAX_GMCP_BYTES) in message
    else:
        raise AssertionError("oversized GMCP subnegotiation was not rejected")


def test_msdp_subnegotiation_is_rejected_at_option_specific_limit():
    from client_core import IAC, SB, OPT_MSDP, MAX_MSDP_BYTES, MudConnection, TelnetProtocolError

    conn = MudConnection("example.org", 4000, EventBus())
    wire = bytes([IAC, SB, OPT_MSDP]) + (b"A" * (MAX_MSDP_BYTES + 1))

    try:
        conn._feed(wire)
    except TelnetProtocolError as exc:
        message = str(exc)
        assert f"option {OPT_MSDP}" in message
        assert str(MAX_MSDP_BYTES) in message
    else:
        raise AssertionError("oversized MSDP subnegotiation was not rejected")


def test_ttype_subnegotiation_uses_small_protocol_specific_ceiling():
    from client_core import IAC, SB, OPT_TTYPE, MAX_TTYPE_BYTES, MudConnection, TelnetProtocolError

    conn = MudConnection("example.org", 4000, EventBus())
    wire = bytes([IAC, SB, OPT_TTYPE]) + (b"X" * (MAX_TTYPE_BYTES + 1))

    try:
        conn._feed(wire)
    except TelnetProtocolError as exc:
        assert f"option {OPT_TTYPE}" in str(exc)
    else:
        raise AssertionError("oversized TTYPE subnegotiation was not rejected")


def test_unknown_subnegotiation_retains_generic_ceiling():
    from client_core import IAC, SB, MAX_SUBNEGOTIATION_BYTES, MudConnection, TelnetProtocolError

    unknown_option = 222
    conn = MudConnection("example.org", 4000, EventBus())
    wire = bytes([IAC, SB, unknown_option]) + (b"X" * (MAX_SUBNEGOTIATION_BYTES + 1))

    try:
        conn._feed(wire)
    except TelnetProtocolError as exc:
        assert f"option {unknown_option}" in str(exc)
    else:
        raise AssertionError("oversized unknown subnegotiation was not rejected")


def test_utf8_split_multibyte_character_is_retained_until_complete():
    from client_core import _decode_text_prefix

    euro = "€".encode("utf-8")
    text, consumed = _decode_text_prefix(b"price=" + euro[:2], "utf-8")

    assert text == "price="
    assert consumed == len(b"price=")

    pending = (b"price=" + euro[:2])[consumed:]
    text2, consumed2 = _decode_text_prefix(pending + euro[2:], "utf-8")
    assert text2 == "€"
    assert consumed2 == len(euro)


def test_invalid_utf8_before_incomplete_tail_does_not_destroy_split_character():
    from client_core import _decode_text_prefix

    euro = "€".encode("utf-8")
    data = b"ok\xff" + euro[:2]
    text, consumed = _decode_text_prefix(data, "utf-8")

    # The complete bad byte is replaced and consumed, but the valid partial
    # Euro sign remains pending for the next network read.
    assert text == "ok\ufffd"
    assert data[consumed:] == euro[:2]

    text2, consumed2 = _decode_text_prefix(data[consumed:] + euro[2:], "utf-8")
    assert text2 == "€"
    assert consumed2 == len(euro)


def test_malformed_utf8_makes_forward_progress_without_stalling_buffer():
    from client_core import _decode_text_prefix

    data = b"A\xff\xfeB"
    text, consumed = _decode_text_prefix(data, "utf-8")

    assert text == "A\ufffd\ufffdB"
    assert consumed == len(data)


def test_utf8_incomplete_tail_then_invalid_continuation_recovers():
    from client_core import _decode_text_prefix

    first = b"hello \xe2\x82"
    text, consumed = _decode_text_prefix(first, "utf-8")
    assert text == "hello "
    assert first[consumed:] == b"\xe2\x82"

    second = first[consumed:] + b"X"
    text2, consumed2 = _decode_text_prefix(second, "utf-8")
    assert "\ufffd" in text2
    assert text2.endswith("X")
    assert consumed2 == len(second)


def test_split_utf8_is_preserved_through_telnet_text_events():
    from client_core import MudConnection

    bus = EventBus()
    seen = []
    bus.on(EventType.TEXT, lambda event: seen.append(event.data))
    conn = MudConnection("example.org", 4000, bus)

    euro = "€".encode("utf-8")
    conn._feed(b"cost " + euro[:2])
    conn._feed(euro[2:] + b"10")

    assert seen == ["cost ", "€10"]
    assert conn._pending_text_bytes == bytearray()


def test_invalid_utf8_before_split_character_recovers_through_telnet_events():
    from client_core import MudConnection

    bus = EventBus()
    seen = []
    bus.on(EventType.TEXT, lambda event: seen.append(event.data))
    conn = MudConnection("example.org", 4000, bus)

    euro = "€".encode("utf-8")
    conn._feed(b"bad:\x80" + euro[:2])
    conn._feed(euro[2:] + b"!")

    assert seen == ["bad:\ufffd", "€!"]
    assert conn._pending_text_bytes == bytearray()


def test_pathologically_deep_gmcp_json_does_not_escape_handler():
    from client_core import MudConnection

    bus = EventBus()
    seen = []
    bus.on(EventType.GMCP, lambda event: seen.append(event.data))
    conn = MudConnection("example.org", 4000, bus)

    deep_json = ("[" * 10000) + "0" + ("]" * 10000)
    payload = ("Core.Deep " + deep_json).encode("utf-8")
    conn._handle_gmcp(payload)

    assert len(seen) == 1
    assert seen[0]["package"] == "Core.Deep"
    assert seen[0]["data"] == deep_json


def test_disconnect_cancellation_still_cleans_transport_state():
    import asyncio
    from client_core import MudConnection

    class BlockingWriter:
        def __init__(self):
            self.closed = False
            self.wait_started = asyncio.Event()

        def close(self):
            self.closed = True

        async def wait_closed(self):
            self.wait_started.set()
            await asyncio.Event().wait()

        def is_closing(self):
            return self.closed

    async def scenario():
        bus = EventBus()
        disconnected = []
        bus.on(EventType.DISCONNECTED, lambda event: disconnected.append(event.data))
        conn = MudConnection("example.org", 4000, bus)
        writer = BlockingWriter()
        conn._writer = writer
        conn._reader = object()

        async def read_forever():
            await asyncio.Event().wait()

        conn._read_task = asyncio.create_task(read_forever())
        task = asyncio.create_task(conn.disconnect())
        await writer.wait_started.wait()
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("disconnect cancellation did not propagate")

        # Let the cancelled read task finish processing its cancellation.
        await asyncio.sleep(0)
        assert conn._reader is None
        assert conn._writer is None
        assert conn._read_task is None
        assert conn._disconnecting is False
        assert disconnected == [None]

    asyncio.run(scenario())


def test_event_bus_subscription_handle_detaches_handler():
    from client_core import EventBus, EventType

    bus = EventBus()
    seen = []
    subscription = bus.on(EventType.TEXT, lambda event: seen.append(event.data))

    bus.emit(EventType.TEXT, "one")
    subscription.close()
    bus.emit(EventType.TEXT, "two")

    assert seen == ["one"]
