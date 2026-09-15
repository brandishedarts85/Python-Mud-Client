from __future__ import annotations

from dataclasses import replace

from ansi_parser import Segment, Style, StyledLine
from session_controller import MudSessionController


def controller(tmp_path):
    return MudSessionController(automation_path=str(tmp_path / "automation.json"))


def test_process_line_keeps_scrollback_and_emits_visible_line(tmp_path):
    session = controller(tmp_path)
    seen = []
    session.on("line", seen.append)
    line = StyledLine([Segment("hello", Style())])

    session._process_line(line)

    assert len(session.scrollback) == 1
    assert seen == [line]


def test_gagged_line_stays_in_scrollback_but_is_not_emitted(tmp_path):
    session = controller(tmp_path)
    seen = []
    session.on("line", seen.append)
    session.engine.add_simple_trigger(r"^secret$", "look", gag=True)
    line = StyledLine([Segment("secret", Style())])

    session._process_line(line)

    assert len(session.scrollback) == 1
    assert seen == []


def test_command_history_is_session_owned(tmp_path):
    session = controller(tmp_path)
    session._history.extend(["look", "north"])
    session._history_pos = len(session._history)

    assert session.history_previous() == "north"
    assert session.history_previous() == "look"
    assert session.history_next() == "north"
    assert session.history_next() == ""


def test_protocol_state_tracks_gmcp_msdp_and_telnet(tmp_path):
    session = controller(tmp_path)
    generation = session._connection_generation

    session._on_gmcp(generation, {"package": "Char.Name", "data": {"name": "Ada"}})
    session._on_msdp(generation, {"variable": "HEALTH", "value": "100"})
    session._on_option_change(generation, {"option": 201, "state": "will"})

    assert session.protocol.gmcp["Char.Name"] == {"name": "Ada"}
    assert session.protocol.msdp["HEALTH"] == "100"
    assert session.protocol.telnet_options[201] == "will"


def test_session_controller_defaults_to_xterm_256color(tmp_path):
    session = controller(tmp_path)
    assert session.terminal_type == "xterm-256color"


class _FakeConnection:
    def __init__(self):
        self.sent = []

    def send_line(self, text):
        self.sent.append(text)


def _connected_session(tmp_path):
    session = controller(tmp_path)
    session.conn = _FakeConnection()
    session._set_state(connected=True, status_text="connected")
    return session


def test_submit_command_locally_echoes_when_server_does_not_echo(tmp_path):
    session = _connected_session(tmp_path)
    seen = []
    session.on("line", seen.append)

    session.submit_command("new")

    assert session.conn.sent == ["new"]
    assert [line.plain_text() for line in seen] == ["new"]
    assert session.scrollback[-1].plain_text() == "new"


def test_submit_command_suppresses_local_echo_when_server_will_echo(tmp_path):
    session = _connected_session(tmp_path)
    generation = session._connection_generation
    session._on_option_change(generation, {"option": 1, "state": "will"})
    seen = []
    session.on("line", seen.append)

    session.submit_command("secret-password")

    assert session.conn.sent == ["secret-password"]
    assert seen == []
    assert len(session.scrollback) == 0


def test_server_wont_echo_restores_local_echo(tmp_path):
    session = _connected_session(tmp_path)
    generation = session._connection_generation
    session._on_option_change(generation, {"option": 1, "state": "will"})
    session._on_option_change(generation, {"option": 1, "state": "wont"})
    seen = []
    session.on("line", seen.append)

    session.submit_command("look")

    assert [line.plain_text() for line in seen] == ["look"]


def test_reconnect_policy_is_configurable(tmp_path):
    session = MudSessionController(
        automation_path=str(tmp_path / "automation.json"),
        auto_reconnect=False, reconnect_base_delay=5.0, reconnect_max_delay=30.0,
    )
    assert session.auto_reconnect is False
    assert session.reconnect_base_delay == 5.0
    assert session.reconnect_max_delay == 30.0


def test_close_cancels_and_awaits_owned_background_tasks(tmp_path):
    import asyncio

    async def scenario():
        session = controller(tmp_path)
        reconnect_cancelled = asyncio.Event()
        tick_cancelled = asyncio.Event()

        async def sleeper(cancelled):
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        session._reconnect_task = asyncio.create_task(sleeper(reconnect_cancelled))
        session._tick_task = asyncio.create_task(sleeper(tick_cancelled))
        await asyncio.sleep(0)

        await session.close()

        assert reconnect_cancelled.is_set()
        assert tick_cancelled.is_set()
        assert session._reconnect_task is None
        assert session._tick_task is None
        assert session.state.status_text == "closed"

    asyncio.run(scenario())


def test_automation_send_locally_echoes_when_server_does_not_echo(tmp_path):
    session = _connected_session(tmp_path)
    seen = []
    session.on("line", seen.append)

    session._send_current_line("n")

    assert session.conn.sent == ["n"]
    assert [line.plain_text() for line in seen] == ["n"]
    assert session.scrollback[-1].plain_text() == "n"
    assert session._history == []


def test_automation_send_suppresses_local_echo_when_server_will_echo(tmp_path):
    session = _connected_session(tmp_path)
    generation = session._connection_generation
    session._on_option_change(generation, {"option": 1, "state": "will"})
    seen = []
    session.on("line", seen.append)

    session._send_current_line("secret")

    assert session.conn.sent == ["secret"]
    assert seen == []
    assert len(session.scrollback) == 0


def test_profile_automation_scope_is_isolated_from_global(tmp_path):
    global_path = tmp_path / "automation.json"
    session = MudSessionController(automation_path=str(global_path))
    session.engine.add_alias("g", "global")
    session.save_automation_now()

    session.set_automation_scope("My MUD")
    assert session.profile_name == "My MUD"
    assert session.engine.aliases == {}
    session.engine.add_alias("p", "profile")
    session.save_automation_now()

    session.set_automation_scope(None)
    assert any(alias.pattern == "g" for alias in session.engine.aliases.values())
    assert not any(alias.pattern == "p" for alias in session.engine.aliases.values())

    session.set_automation_scope("My MUD")
    assert any(alias.pattern == "p" for alias in session.engine.aliases.values())
    assert not any(alias.pattern == "g" for alias in session.engine.aliases.values())


def test_session_records_trigger_fire_log(tmp_path):
    session = controller(tmp_path)
    session.engine.add_simple_trigger(r"^ping$", "pong")
    session._process_line(StyledLine([Segment("ping", Style())]))

    assert len(session.automation_fire_log) == 1
    event = session.automation_fire_log[-1]
    assert event.pattern == r"^ping$"
    assert event.response == "pong"


def test_malformed_automation_json_does_not_crash_session_startup(tmp_path):
    import asyncio

    path = tmp_path / "automation.json"
    original = '{"enabled": true, "triggers": ['
    path.write_text(original, encoding="utf-8")

    session = MudSessionController(automation_path=str(path))

    assert session.engine.aliases == {}
    assert session.engine.triggers == {}
    assert session.automation_load_error is not None
    assert "invalid JSON" in session.automation_load_error
    assert path.read_text(encoding="utf-8") == original

    seen = []
    session.on("system", lambda line: seen.append(line.plain_text()))

    async def scenario():
        await session.start()
        await session.close()

    asyncio.run(scenario())

    assert any("automation not loaded" in line for line in seen)
    assert any("invalid JSON" in line for line in seen)


def test_invalid_persisted_trigger_regex_falls_back_to_empty_automation(tmp_path):
    import json

    path = tmp_path / "automation.json"
    data = {
        "enabled": True,
        "aliases": [{"pattern": "l", "expansion": "look", "enabled": True}],
        "triggers": [
            {
                "pattern": "(",
                "response": "north",
                "gag": False,
                "one_shot": False,
                "cooldown_s": 0.0,
                "case_sensitive": False,
                "priority": 0,
                "enabled": True,
                "style": None,
            }
        ],
    }
    original = json.dumps(data)
    path.write_text(original, encoding="utf-8")

    session = MudSessionController(automation_path=str(path))

    # Validation is transactional: the valid alias before the bad trigger is
    # not partially applied, and construction still succeeds.
    assert session.engine.aliases == {}
    assert session.engine.triggers == {}
    assert session.automation_load_error is not None
    assert "invalid regex" in session.automation_load_error
    assert path.read_text(encoding="utf-8") == original


def test_text_then_empty_eor_prompt_boundary_flushes_buffered_prompt_text(tmp_path):
    session = controller(tmp_path)
    generation = session._connection_generation
    seen = []
    session.on("line", seen.append)

    # This is the transport event sequence produced when ordinary prompt text
    # is followed by IAC EOR: TEXT carries the bytes, PROMPT marks the boundary.
    session._on_incoming(generation, "prompt> ", False)
    assert seen == []

    session._on_incoming(generation, "", True)

    assert [line.plain_text() for line in seen] == ["prompt> "]


def test_protocol_snapshots_evict_oldest_entries_when_bounded(tmp_path):
    session = controller(tmp_path)
    session.MAX_PROTOCOL_SNAPSHOT_ENTRIES = 3
    generation = session._connection_generation

    for index in range(5):
        session._on_gmcp(
            generation,
            {"package": f"Pkg.{index}", "data": index},
        )

    assert list(session.protocol.gmcp) == ["Pkg.2", "Pkg.3", "Pkg.4"]
    assert len(session.protocol.gmcp) == 3


def test_protocol_snapshot_updates_existing_key_without_eviction(tmp_path):
    session = controller(tmp_path)
    session.MAX_PROTOCOL_SNAPSHOT_ENTRIES = 2
    generation = session._connection_generation

    session._on_msdp(generation, {"A": "1"})
    session._on_msdp(generation, {"B": "2"})
    session._on_msdp(generation, {"A": "updated"})

    assert session.protocol.msdp == {"A": "updated", "B": "2"}


def test_static_automation_loader_returns_error_without_mutating_controller(tmp_path):
    from automation import AutomationEngine

    path = tmp_path / "automation.json"
    path.write_text('{"triggers": [', encoding="utf-8")
    fresh = AutomationEngine(send_fn=lambda _line: None)

    result = MudSessionController._load_automation_safely(str(path), fresh)

    assert result.engine is fresh
    assert result.alias_count == 0
    assert result.trigger_count == 0
    assert result.error is not None
    assert fresh.aliases == {}
    assert fresh.triggers == {}


def test_session_discards_overlong_ansi_line_and_recovers(tmp_path):
    from ansi_parser import AnsiParser

    session = controller(tmp_path)
    session.ansi = AnsiParser(max_line_chars=4)
    generation = session._connection_generation
    systems = []
    visible = []
    session.on("system", lambda line: systems.append(line.plain_text()))
    session.on("line", lambda line: visible.append(line.plain_text()))

    session._on_incoming(generation, "1234", False)
    session._on_incoming(generation, "5", False)
    session._on_incoming(generation, "ok\n", False)

    assert any("ANSI input discarded" in text for text in systems)
    assert visible[-1] == "ok"


def test_command_history_is_bounded(tmp_path):
    session = _connected_session(tmp_path)
    session.MAX_COMMAND_HISTORY = 3

    for command in ("one", "two", "three", "four", "five"):
        session.submit_command(command)

    assert session._history == ["three", "four", "five"]
    assert session._history_pos == 3


def test_close_invalidates_late_transport_generation_events(tmp_path):
    import asyncio

    async def scenario():
        session = controller(tmp_path)
        old_generation = session._connection_generation
        systems = []
        session.on("system", lambda line: systems.append(line.plain_text()))

        await session.close()
        assert session.state.status_text == "closed"

        # Simulate callbacks queued by the retired EventBus arriving after
        # teardown.  They must not resurrect "disconnected" state or report a
        # stale transport error.
        session._on_disconnected(old_generation)
        session._on_error(old_generation, "late error")

        assert session.state.status_text == "closed"
        assert not any("late error" in line for line in systems)

    asyncio.run(scenario())


def test_close_is_idempotent_and_leaves_no_owned_tasks(tmp_path):
    import asyncio

    async def scenario():
        session = controller(tmp_path)
        await session.start()
        await session.close()
        await session.close()

        assert session._tick_task is None
        assert session._reconnect_task is None
        assert session.conn is None
        assert session.state.status_text == "closed"

    asyncio.run(scenario())


def test_automatic_reconnect_task_does_not_cancel_itself(tmp_path, monkeypatch):
    import asyncio
    import session_controller as session_module
    from client_core import EventType

    class FakeMudConnection:
        def __init__(self, host, port, bus, terminal_type="xterm-256color"):
            self.host = host
            self.port = port
            self.bus = bus

        async def connect(self):
            # Cancellation caused by connect()->_cancel_reconnect_task() is
            # delivered at this await on the broken implementation.
            await asyncio.sleep(0)
            self.bus.emit(EventType.CONNECTED, {"host": self.host, "port": self.port})

        async def disconnect(self):
            return None

        def send_line(self, _line):
            return None

    async def scenario():
        monkeypatch.setattr(session_module, "MudConnection", FakeMudConnection)
        session = MudSessionController(
            host="mud.example",
            port=4000,
            automation_path=str(tmp_path / "automation.json"),
            reconnect_base_delay=0.001,
            reconnect_max_delay=0.001,
        )
        task = asyncio.create_task(session._reconnect_after(0))
        session._reconnect_task = task
        await task

        assert session.state.connected is True
        assert session.state.status_text == "connected to mud.example:4000"
        assert session._reconnect_task is None
        await session.close()

    asyncio.run(scenario())


def test_invalid_client_trigger_regex_is_reported_not_raised(tmp_path):
    session = controller(tmp_path)
    messages = []
    session.on("system", lambda line: messages.append(line.plain_text()))

    session.submit_command("#trigger [ = north")

    assert session.engine.triggers == {}
    assert any("could not add trigger" in message for message in messages)


def test_session_listener_failure_is_isolated(tmp_path):
    session = controller(tmp_path)
    seen = []

    def broken(_payload):
        raise RuntimeError("listener boom")

    session.on("line", broken)
    session.on("line", seen.append)
    line = StyledLine([Segment("still delivered", Style())])

    session._process_line(line)

    assert seen == [line]


def test_command_history_restores_in_progress_draft(tmp_path):
    session = controller(tmp_path)
    session._history.extend(["look", "north"])
    session._history_pos = len(session._history)

    assert session.history_previous("say hel") == "north"
    assert session.history_previous() == "look"
    assert session.history_next() == "north"
    assert session.history_next() == "say hel"


def test_protocol_event_feed_is_bounded_and_large_payloads_are_truncated(tmp_path):
    session = controller(tmp_path)
    generation = session._connection_generation

    for index in range(250):
        session._on_option_change(generation, {"option": index % 256, "state": "will"})

    assert len(session.protocol.recent_events) == 200

    session._on_gmcp(
        generation,
        {"package": "Big.Payload", "data": {"text": "x" * 10000}},
    )
    event = session.protocol.recent_events[-1]
    assert event["kind"] == "GMCP"
    assert isinstance(event["data"], dict)
    assert "truncated" in event["data"]
    assert len(event["data"]["truncated"]) <= 4097


def test_protocol_notice_is_recorded_without_overwriting_option_state(tmp_path):
    session = controller(tmp_path)
    generation = session._connection_generation
    session._on_option_change(generation, {"option": 1, "state": "will"})

    notice = {
        "kind": "negotiation-churn",
        "option": 1,
        "command": "WILL",
        "count": 25,
    }
    session._on_protocol_notice(generation, notice)

    assert session.protocol.telnet_options[1] == "will"
    assert session.protocol.recent_events[-1] == {
        "kind": "Protocol",
        "data": notice,
    }


def test_profile_scope_replacement_updates_command_pipeline_alias_source(tmp_path):
    session = _connected_session(tmp_path)
    session.engine.add_alias("x", "global")

    session.set_automation_scope("Profile A")
    session.engine.add_alias("x", "profile")
    session.submit_command("x")

    assert session.conn.sent == ["profile"]


def test_session_subscription_handle_detaches_listener(tmp_path):
    session = controller(tmp_path)
    seen = []
    subscription = session.on("system", seen.append)

    session._system("one")
    subscription.close()
    session._system("two")

    assert len(seen) == 1
    assert seen[0].plain_text() == "one"


def test_close_releases_transport_bus_subscriptions(tmp_path):
    import asyncio
    from client_core import EventBus, EventType

    async def scenario():
        session = controller(tmp_path)
        bus = EventBus()
        generation = session._connection_generation
        session._wire_bus(bus, generation)
        assert session._bus_subscriptions

        await session.close()
        before = len(session.scrollback)
        bus.emit(EventType.TEXT, "late text\n")

        assert session._bus_subscriptions == []
        assert len(session.scrollback) == before
        assert session.state.status_text == "closed"

    asyncio.run(scenario())


def test_local_echo_setting_can_disable_client_side_echo(tmp_path):
    from types import SimpleNamespace

    session = MudSessionController(
        automation_path=str(tmp_path / "automation.json"),
        local_echo_enabled=False,
    )
    sent = []
    session.conn = SimpleNamespace(send_line=sent.append)
    session.state = replace(session.state, connected=True)

    session.submit_command("look")

    assert sent == ["look"]
    assert len(session.scrollback) == 0


def test_scrollback_limit_can_be_resized_without_losing_newest_lines(tmp_path):
    session = MudSessionController(
        automation_path=str(tmp_path / "automation.json"),
        scrollback_lines=5,
    )
    for number in range(5):
        session._process_line(StyledLine([Segment(f"line-{number}", Style())]))

    session.set_scrollback_limit(3)

    assert session.scrollback.max_lines == 3
    assert [line.plain_text() for line in session.scrollback.tail(3)] == [
        "line-2",
        "line-3",
        "line-4",
    ]


def test_session_variables_expand_manual_alias_macro_and_automation_output(tmp_path):
    from command_pipeline import CommandSource

    session = _connected_session(tmp_path)
    session.set_variable("target", "goblin")
    session.engine.add_alias("k", "kill ${target}")

    session.submit_command("k")
    session.submit_command("say ${target}", source=CommandSource.MACRO)
    session._send_current_line("consider ${target}")

    assert session.conn.sent == ["kill goblin", "say goblin", "consider goblin"]


def test_profile_variable_scope_is_isolated_from_global(tmp_path):
    global_path = tmp_path / "automation.json"
    session = MudSessionController(automation_path=str(global_path))
    session.set_variable("who", "global")

    session.set_automation_scope("My MUD")
    assert session.variables.as_dict() == {}
    session.set_variable("who", "profile")

    session.set_automation_scope(None)
    assert session.variables.get("who") == "global"

    session.set_automation_scope("My MUD")
    assert session.variables.get("who") == "profile"


def test_failed_variable_save_rolls_back_live_state(tmp_path, monkeypatch):
    import session_controller as session_module

    session = controller(tmp_path)
    session.set_variable("safe", "old")

    def fail_save(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(session_module, "save_variables", fail_save)

    import pytest
    with pytest.raises(OSError, match="disk full"):
        session.set_variable("safe", "new")

    assert session.variables.get("safe") == "old"


def test_variable_rename_refuses_to_overwrite_existing_name(tmp_path):
    session = controller(tmp_path)
    session.set_variable("first", "a")
    session.set_variable("second", "b")

    import pytest
    with pytest.raises(ValueError, match="already exists"):
        session.replace_variable("first", "second", "new")

    assert session.variables.as_dict() == {"first": "a", "second": "b"}


def test_client_variable_commands_create_typed_values_and_substitute(tmp_path):
    session = _connected_session(tmp_path)

    session.submit_command("#set target = orc")
    session.submit_command("#set retries:int = 3")
    session.submit_command("#set enabled:bool = yes")
    session.submit_command("say ${target} ${retries} ${enabled} ${missing}")

    assert session.variables.as_dict() == {
        "target": "orc",
        "retries": 3,
        "enabled": True,
    }
    assert session.conn.sent == ["say orc 3 true ${missing}"]
