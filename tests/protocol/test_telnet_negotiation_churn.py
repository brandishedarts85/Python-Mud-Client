import client_core
from client_core import (
    DONT,
    DO,
    IAC,
    MAX_NEGOTIATIONS_PER_OPTION_WINDOW,
    NEGOTIATION_CHURN_SUPPRESS_SECONDS,
    OPT_ECHO,
    OPT_NAWS,
    OPT_SGA,
    WILL,
    WONT,
    EventBus,
    EventType,
    MudConnection,
)


class FakeWriter:
    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(data)

    def is_closing(self):
        return False


def connected_connection():
    bus = EventBus()
    conn = MudConnection("example.org", 4000, bus)
    writer = FakeWriter()
    conn._writer = writer
    return conn, bus, writer


def test_duplicate_accepted_will_is_idempotent_and_not_reacknowledged():
    conn, bus, writer = connected_connection()
    changes = []
    bus.on(EventType.OPTION_CHANGE, lambda event: changes.append(event.data))

    conn._handle_negotiation(WILL, OPT_ECHO)
    conn._handle_negotiation(WILL, OPT_ECHO)

    assert writer.writes == [bytes([IAC, DO, OPT_ECHO])]
    assert changes == [{"option": OPT_ECHO, "state": "will"}]
    assert OPT_ECHO in conn._remote_options


def test_duplicate_accepted_do_naws_does_not_repeat_will_or_window_size():
    conn, bus, writer = connected_connection()
    changes = []
    bus.on(EventType.OPTION_CHANGE, lambda event: changes.append(event.data))

    conn._handle_negotiation(DO, OPT_NAWS)
    first_writes = list(writer.writes)
    assert len(first_writes) == 2  # WILL NAWS, then one NAWS subnegotiation.

    conn._handle_negotiation(DO, OPT_NAWS)

    assert writer.writes == first_writes
    assert changes == [{"option": OPT_NAWS, "state": "do"}]


def test_legitimate_disable_then_reenable_still_negotiates_normally():
    conn, bus, writer = connected_connection()
    changes = []
    bus.on(EventType.OPTION_CHANGE, lambda event: changes.append(event.data))

    conn._handle_negotiation(WILL, OPT_ECHO)
    conn._handle_negotiation(WONT, OPT_ECHO)
    conn._handle_negotiation(WILL, OPT_ECHO)

    assert writer.writes == [
        bytes([IAC, DO, OPT_ECHO]),
        bytes([IAC, DO, OPT_ECHO]),
    ]
    assert changes == [
        {"option": OPT_ECHO, "state": "will"},
        {"option": OPT_ECHO, "state": "wont"},
        {"option": OPT_ECHO, "state": "will"},
    ]


def test_churn_on_one_option_is_suppressed_once_without_blocking_other_options(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(client_core.time, "monotonic", lambda: now[0])

    conn, bus, writer = connected_connection()
    notices = []
    bus.on(EventType.PROTOCOL_NOTICE, lambda event: notices.append(event.data))

    unsupported = 222
    for _ in range(MAX_NEGOTIATIONS_PER_OPTION_WINDOW + 1):
        conn._handle_negotiation(WILL, unsupported)

    # The first budgeted messages are handled, and the first over-budget one
    # begins suppression without sending another refusal.
    assert len(writer.writes) == MAX_NEGOTIATIONS_PER_OPTION_WINDOW
    assert all(write == bytes([IAC, DONT, unsupported]) for write in writer.writes)
    assert len(notices) == 1
    assert notices[0]["kind"] == "negotiation-churn"
    assert notices[0]["option"] == unsupported

    for _ in range(100):
        conn._handle_negotiation(WILL, unsupported)
    assert len(writer.writes) == MAX_NEGOTIATIONS_PER_OPTION_WINDOW
    assert len(notices) == 1

    # Suppression is scoped to one option, not the whole Telnet parser.
    conn._handle_negotiation(WILL, OPT_SGA)
    assert writer.writes[-1] == bytes([IAC, DO, OPT_SGA])


def test_churn_suppression_expires_and_gives_peer_a_clean_window(monkeypatch):
    now = [200.0]
    monkeypatch.setattr(client_core.time, "monotonic", lambda: now[0])

    conn, bus, writer = connected_connection()
    notices = []
    bus.on(EventType.PROTOCOL_NOTICE, lambda event: notices.append(event.data))
    unsupported = 223

    for _ in range(MAX_NEGOTIATIONS_PER_OPTION_WINDOW + 1):
        conn._handle_negotiation(WILL, unsupported)
    writes_at_suppression = len(writer.writes)

    now[0] += NEGOTIATION_CHURN_SUPPRESS_SECONDS + 0.001
    conn._handle_negotiation(WILL, unsupported)

    assert len(writer.writes) == writes_at_suppression + 1
    assert writer.writes[-1] == bytes([IAC, DONT, unsupported])
    assert len(notices) == 1
    assert len(conn._negotiation_times[unsupported]) == 1


def test_transport_reset_clears_churn_history_and_suppression(monkeypatch):
    now = [300.0]
    monkeypatch.setattr(client_core.time, "monotonic", lambda: now[0])

    conn, _bus, _writer = connected_connection()
    unsupported = 224
    for _ in range(MAX_NEGOTIATIONS_PER_OPTION_WINDOW + 1):
        conn._handle_negotiation(WILL, unsupported)

    assert unsupported in conn._negotiation_suppressed_until
    conn._reset_transport_state()
    assert conn._negotiation_times == {}
    assert conn._negotiation_suppressed_until == {}
