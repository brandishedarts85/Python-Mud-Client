"""UI-neutral session orchestration for the Python MUD Client.

This module is the migration seam between the existing backend and any UI.
It owns one logical MUD session and deliberately has no Qt/Textual imports.

Transport/parser/automation/persistence remain in their existing modules:

    client_core -> ansi_parser -> automation/persistence
                        |
                        v
                MudSessionController
                        |
                        v
                     any UI
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from command_pipeline import CommandPipeline, CommandRequest, CommandResult, CommandSource
from lifecycle import Subscription, TaskOwner

from ansi_parser import AnsiParseLimitError, AnsiParser, Scrollback, Segment, Style, StyledLine
from automation import AutomationEngine, TriggerFireEvent
from client_core import EventBus, EventType, MudConnection
from mapper import MapperRepository
from mapper_adapter import (
    MapperAdapterCapability,
    adapter_choices,
    create_mapper_adapter,
    mapper_adapter_capabilities as get_mapper_adapter_capabilities,
)
from mapper_walker import MapperWalker, WalkStatus
from persistence import (
    DEFAULT_AUTOMATION_PATH,
    load_automation,
    load_variables_safely,
    profile_automation_path,
    profile_variables_path,
    profile_mapper_path,
    save_automation,
    save_profile,
    save_variables,
)
from variables import VariableStore, VariableValue, parse_typed_value


Listener = Callable[[Any], None]

logger = logging.getLogger("mudclient.session")


@dataclass(frozen=True)
class SessionState:
    host: str | None = None
    port: int | None = None
    connected: bool = False
    connecting: bool = False
    reconnecting: bool = False
    status_text: str = "disconnected"


@dataclass
class ProtocolState:
    gmcp: dict[str, Any] = field(default_factory=dict)
    msdp: dict[str, Any] = field(default_factory=dict)
    telnet_options: dict[int, str] = field(default_factory=dict)
    recent_events: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=200)
    )

    def record_event(self, kind: str, data: Any, *, max_chars: int = 4096) -> None:
        """Retain a bounded diagnostic event for the protocol inspector.

        Normal protocol payloads remain structured for the rich inspector.
        Pathologically large payloads are replaced by a bounded textual
        summary so a 200-entry event history cannot retain tens of megabytes.
        """
        try:
            serialized = json.dumps(
                data, sort_keys=True, default=str, separators=(",", ":")
            )
        except (TypeError, ValueError, RecursionError):
            serialized = repr(data)

        if len(serialized) <= max_chars:
            retained = data
        else:
            retained = {
                "truncated": serialized[:max_chars] + "…",
                "original_chars_at_least": len(serialized),
            }
        self.recent_events.append({"kind": kind, "data": retained})


@dataclass(frozen=True)
class AutomationLoadResult:
    engine: AutomationEngine
    alias_count: int
    trigger_count: int
    error: str | None = None


class MudSessionController:
    """Own one MUD session without owning any presentation widgets."""

    RECONNECT_BASE_DELAY = 3.0
    RECONNECT_MAX_DELAY = 60.0
    PROMPT_IDLE_TIMEOUT = 0.3
    MAX_PROTOCOL_SNAPSHOT_ENTRIES = 512
    MAX_COMMAND_HISTORY = 2000

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        *,
        scrollback_lines: int = 10_000,
        automation_path: str = DEFAULT_AUTOMATION_PATH,
        variables_path: str | None = None,
        mapper_path: str | None = None,
        terminal_type: str = "xterm-256color",
        auto_reconnect: bool = True,
        reconnect_base_delay: float = 3.0,
        reconnect_max_delay: float = 60.0,
        local_echo_enabled: bool = True,
    ) -> None:
        self.state = SessionState(host=host, port=port)
        self.protocol = ProtocolState()

        self.ansi = AnsiParser()
        self.scrollback = Scrollback(max_lines=scrollback_lines)
        self.bus = EventBus()
        self.conn: MudConnection | None = None

        self.global_automation_path = automation_path
        self.automation_path = automation_path
        inferred_variables_path = str(Path(automation_path).with_name("variables.json"))
        self.global_variables_path = variables_path or inferred_variables_path
        self.variables_path = self.global_variables_path
        inferred_mapper_path = str(Path(automation_path).with_name("mapper.sqlite3"))
        self.global_mapper_path = mapper_path or inferred_mapper_path
        self.mapper_path = self.global_mapper_path
        self.mapper = MapperRepository(self.mapper_path)
        self.mapper_adapter_key = "generic-gmcp"
        self.mapper_adapter = create_mapper_adapter(self.mapper_adapter_key)
        self.current_room_id: int | None = None
        self.mapper_walker = MapperWalker(
            self._send_mapper_walk_step,
            reroute=self._mapper_reroute,
            status_changed=lambda _status: self._emit("mapper", None),
        )
        self.variables, self.variables_load_error = load_variables_safely(self.variables_path)
        self._variables_load_reported = False
        self.profile_name: str | None = None
        self.terminal_type = terminal_type
        self.auto_reconnect = bool(auto_reconnect)
        self.reconnect_base_delay = float(reconnect_base_delay)
        self.reconnect_max_delay = float(reconnect_max_delay)
        self.local_echo_enabled = bool(local_echo_enabled)
        if self.reconnect_base_delay <= 0:
            raise ValueError("reconnect_base_delay must be > 0")
        if self.reconnect_max_delay < self.reconnect_base_delay:
            raise ValueError("reconnect_max_delay must be >= reconnect_base_delay")
        load_result = self._load_automation_safely(
            automation_path,
            self._new_automation_engine(),
        )
        self._apply_automation_load_result(load_result)
        self._automation_load_reported = False
        self.automation_fire_log: deque[TriggerFireEvent] = deque(maxlen=200)
        self.last_styled_line: StyledLine | None = None

        self._history: list[str] = []
        self._history_pos = 0
        self._history_draft = ""

        self._tick_task: asyncio.Task | None = None
        self._last_text_time = 0.0

        self._manual_disconnect = False
        self._shutting_down = False
        self._reconnect_task: asyncio.Task | None = None
        self._reconnect_attempt = 0
        self._connection_generation = 0
        self._task_owner = TaskOwner("mud-session")
        self._bus_subscriptions: list[Subscription] = []

        self._listeners: dict[str, list[Listener]] = {
            "line": [],
            "system": [],
            "state": [],
            "gmcp": [],
            "msdp": [],
            "telnet": [],
            "automation": [],
            "automation_fire": [],
            "variables": [],
            "mapper": [],
        }

        self.command_pipeline = CommandPipeline(
            is_connected=lambda: self.conn is not None and self.state.connected,
            server_echo_enabled=lambda: self.server_echo_enabled,
            send_line=self._send_transport_line,
            expand_aliases=lambda command: self.engine.on_command(command),
            expand_variables=lambda command: self.variables.substitute(command),
            local_echo=self._local_echo,
            record_history=self._record_history,
            handle_client_command=self._handle_client_command,
            notify_not_connected=lambda: self._system("[not connected]"),
        )

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------

    def on(self, event: str, listener: Listener) -> Subscription:
        if event not in self._listeners:
            raise ValueError(f"unknown session event: {event}")
        if listener not in self._listeners[event]:
            self._listeners[event].append(listener)
        return Subscription(lambda: self.off(event, listener))

    def off(self, event: str, listener: Listener) -> None:
        listeners = self._listeners.get(event)
        if not listeners:
            return
        with suppress(ValueError):
            listeners.remove(listener)

    def _emit(self, event: str, payload: Any) -> None:
        # Presentation/plugin listeners are outside the session core.  A bad
        # listener must not abort transport processing, reconnect state, or an
        # automation action that has already been accepted.
        for listener in tuple(self._listeners[event]):
            try:
                listener(payload)
            except Exception:
                logger.exception("session listener raised for %s", event)

    def _set_state(self, **changes: Any) -> None:
        self.state = replace(self.state, **changes)
        self._emit("state", self.state)

    @property
    def display_name(self) -> str:
        if self.state.host and self.state.port:
            return f"{self.state.host}:{self.state.port}"
        return "New Session"

    @property
    def automation_scope_label(self) -> str:
        return self.profile_name or "Global"

    @property
    def variable_scope_label(self) -> str:
        return self.profile_name or "Global"

    @property
    def mapper_scope_label(self) -> str:
        return self.profile_name or "Global"

    def _report_variable_load_status(self) -> None:
        if self._variables_load_reported:
            return
        if self.variables_load_error:
            self._system(
                f"[variables not loaded from {self.variables_path}: "
                f"{self.variables_load_error}]"
            )
        elif len(self.variables):
            self._system(
                f"[loaded {len(self.variables)} variable(s) from {self.variables_path}]"
            )
        self._variables_load_reported = True

    def set_variable_scope(self, profile_name: str | None) -> None:
        normalized_name = str(profile_name).strip() if profile_name is not None else None
        if not normalized_name:
            normalized_name = None
            target_path = self.global_variables_path
        else:
            root = Path(self.global_variables_path).parent / "profiles_data"
            target_path = profile_variables_path(normalized_name, root=root)

        if target_path == self.variables_path:
            return
        variables, error = load_variables_safely(target_path)
        self.variables_path = target_path
        self.variables = variables
        self.variables_load_error = error
        self._variables_load_reported = False
        self._emit("variables", None)

    def set_variable(self, name: str, value: VariableValue) -> None:
        before = self.variables.as_dict()
        self.variables.set(name, value)
        try:
            save_variables(self.variables, self.variables_path)
        except Exception:
            self.variables.replace_all(before)
            raise
        self._emit("variables", None)

    def remove_variable(self, name: str) -> bool:
        before = self.variables.as_dict()
        removed = self.variables.remove(name)
        if not removed:
            return False
        try:
            save_variables(self.variables, self.variables_path)
        except Exception:
            self.variables.replace_all(before)
            raise
        self._emit("variables", None)
        return True

    def replace_variable(self, old_name: str, new_name: str, value: VariableValue) -> None:
        before = self.variables.as_dict()
        if old_name != new_name and new_name in self.variables:
            raise ValueError(f"variable {new_name!r} already exists")
        if old_name != new_name:
            self.variables.remove(old_name)
        self.variables.set(new_name, value)
        try:
            save_variables(self.variables, self.variables_path)
        except Exception:
            self.variables.replace_all(before)
            raise
        self._emit("variables", None)

    def save_variables_now(self) -> None:
        save_variables(self.variables, self.variables_path)
        self._emit("variables", None)

    def _new_automation_engine(self) -> AutomationEngine:
        engine = AutomationEngine(send_fn=self._send_current_line)
        engine.add_trigger_fire_hook(self._on_trigger_fire)
        return engine

    @staticmethod
    def _load_automation_safely(
        path: str,
        engine: AutomationEngine,
    ) -> AutomationLoadResult:
        """Validate and load one automation file into a fresh engine.

        ``load_automation`` already validates the entire JSON document before
        mutating the engine.  This wrapper adds the application fail-soft
        policy without weakening that atomic validation rule: on any persisted
        data/read error the caller receives the still-empty fresh engine and an
        error string.  The source file is never rewritten here.
        """
        try:
            alias_count, trigger_count = load_automation(engine, path)
        except (ValueError, OSError) as exc:
            return AutomationLoadResult(
                engine=engine,
                alias_count=0,
                trigger_count=0,
                error=str(exc),
            )

        return AutomationLoadResult(
            engine=engine,
            alias_count=alias_count,
            trigger_count=trigger_count,
            error=None,
        )

    def _apply_automation_load_result(self, result: AutomationLoadResult) -> None:
        self.engine = result.engine
        self._loaded_alias_count = result.alias_count
        self._loaded_trigger_count = result.trigger_count
        self.automation_load_error = result.error

    def _report_automation_load_status(self) -> None:
        if self._automation_load_reported:
            return

        if self.automation_load_error:
            self._system(
                f"[automation not loaded from {self.automation_path}: "
                f"{self.automation_load_error}]"
            )
        elif self._loaded_alias_count or self._loaded_trigger_count:
            self._system(
                f"[loaded {self._loaded_alias_count} alias(es) and "
                f"{self._loaded_trigger_count} trigger(s) from "
                f"{self.automation_path}]"
            )

        self._automation_load_reported = True

    def _on_trigger_fire(self, event: TriggerFireEvent) -> None:
        self.automation_fire_log.append(event)
        self._emit("automation_fire", event)
        trigger = self.engine.triggers.get(event.trigger_id)
        if trigger is not None and not trigger.enabled:
            # One-shot triggers disable themselves during fire(). Refresh the
            # editor immediately so its On state does not look stale.
            self._emit("automation", None)

    def set_automation_scope(self, profile_name: str | None) -> None:
        """Switch persistence-safe automation to a profile-specific file.

        Unnamed/manual sessions continue using the legacy global automation.json.
        Saved profiles use deterministic files under profiles_data/.  The live
        engine is replaced only after the target file validates successfully.
        """
        normalized_name = str(profile_name).strip() if profile_name is not None else None
        if not normalized_name:
            normalized_name = None
            target_path = self.global_automation_path
        else:
            root = Path(self.global_automation_path).parent / "profiles_data"
            target_path = profile_automation_path(normalized_name, root=root)

        automation_changed = not (
            target_path == self.automation_path and normalized_name == self.profile_name
        )
        self.profile_name = normalized_name
        if automation_changed:
            self.automation_path = target_path
            load_result = self._load_automation_safely(
                target_path,
                self._new_automation_engine(),
            )
            self._apply_automation_load_result(load_result)
            self._automation_load_reported = False
            self.automation_fire_log.clear()
            self._emit("automation", None)
        self.set_variable_scope(normalized_name)
        self.set_mapper_scope(normalized_name)


    def set_mapper_scope(self, profile_name: str | None) -> None:
        normalized = str(profile_name).strip() if profile_name is not None else None
        if not normalized:
            target = self.global_mapper_path
        else:
            root = Path(self.global_mapper_path).parent / "profiles_data"
            target = profile_mapper_path(normalized, root=root)
        if target == self.mapper_path:
            return
        old = self.mapper
        replacement = MapperRepository(target)
        self.mapper = replacement
        self.mapper_path = target
        self.mapper_walker.cancel("cancelled: mapper scope changed")
        self.current_room_id = None
        old.close()
        self._emit("mapper", None)

    @property
    def mapper_adapter_choices(self) -> tuple[tuple[str, str], ...]:
        return adapter_choices()

    @property
    def mapper_adapter_capabilities(self) -> frozenset[MapperAdapterCapability]:
        return get_mapper_adapter_capabilities(self.mapper_adapter_key)

    def mapper_adapter_supports(self, capability: MapperAdapterCapability | str) -> bool:
        try:
            normalized = (
                capability
                if isinstance(capability, MapperAdapterCapability)
                else MapperAdapterCapability(str(capability))
            )
        except ValueError:
            return False
        return normalized in self.mapper_adapter_capabilities

    def set_mapper_adapter(self, key: str) -> None:
        self.mapper_walker.cancel("cancelled: mapper adapter changed")
        replacement = create_mapper_adapter(key)
        self.mapper_adapter_key = str(key)
        self.mapper_adapter = replacement
        self._emit("mapper", None)

    def mapper_changed(self) -> None:
        self._emit("mapper", None)

    def _apply_mapper_observation(self, observation) -> None:
        if observation is None:
            return
        try:
            room = self.mapper.apply_observation(observation)
        except Exception as exc:
            logger.warning("mapper observation rejected: %s", exc)
            self.protocol.record_event("Mapper", {"error": str(exc)})
            return
        self.current_room_id = room.id
        self.mapper_walker.note_room(room.id)
        self._emit("mapper", None)

    @property
    def mapper_walk_status(self) -> WalkStatus:
        return self.mapper_walker.status

    def _send_mapper_walk_step(self, command: str) -> bool:
        result = self.send_mapper_command(command)
        return bool(result.sent) and not result.rejected_disconnected

    def _validate_auto_walk_steps(self, steps) -> None:
        if not self.mapper_adapter_supports(MapperAdapterCapability.ROOM_IDENTITY):
            raise ValueError("automatic walking requires a room-identity adapter")
        # Every destination must have a stable external identity. Prompts alone
        # are never accepted as proof that movement succeeded.
        for step in steps:
            room = self.mapper.get_room(step.destination_room_id)
            if not room.external_id:
                raise ValueError(f"room {room.id} has no stable external identity")

    def _mapper_reroute(self, source_room_id: int, destination_room_id: int):
        """Return a safe replacement route from a positively observed room.

        The walker calls this only after receiving a stable room observation.
        Routing remains controller-owned so the generic walker never gains
        SQLite access or weakens the room-identity policy.
        """
        if not self.mapper_adapter_supports(MapperAdapterCapability.ROOM_IDENTITY):
            return None
        try:
            steps = self.mapper.find_route(int(source_room_id), int(destination_room_id))
            self._validate_auto_walk_steps(steps)
        except (KeyError, ValueError):
            return None
        return steps

    def start_mapper_walk(self, destination_room_id: int) -> WalkStatus:
        if self.current_room_id is None:
            raise ValueError("current room is unknown")
        steps = self.mapper.find_route(self.current_room_id, int(destination_room_id))
        if not steps:
            return self.mapper_walker.start(
                steps, source_room_id=self.current_room_id, destination_room_id=int(destination_room_id)
            )
        self._validate_auto_walk_steps(steps)
        return self.mapper_walker.start(
            steps,
            source_room_id=self.current_room_id,
            destination_room_id=int(destination_room_id),
        )

    def cancel_mapper_walk(self, reason: str = "cancelled by user") -> WalkStatus:
        return self.mapper_walker.cancel(reason)

    def send_mapper_command(self, command: str) -> CommandResult:
        return self.dispatch_command(CommandRequest(command, CommandSource.MAPPER))

    def set_automation_enabled(self, enabled: bool) -> None:
        self.engine.set_master_enabled(enabled)
        self._emit("automation", None)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        # start() runs after the UI bridge has subscribed, so startup warnings
        # are visible instead of being lost during __init__.
        self._report_automation_load_status()
        self._report_variable_load_status()

        if self._tick_task is None or self._tick_task.done():
            self._tick_task = self._task_owner.create(
                self._tick_loop(),
                name="mud-session-tick-loop",
            )

        if self.state.host and self.state.port is not None:
            await self.connect(self.state.host, self.state.port)

    async def connect(self, host: str, port: int) -> None:
        if self._shutting_down:
            return

        self._manual_disconnect = False
        self._cancel_reconnect_task()

        old_conn = self.conn
        self._connection_generation += 1
        generation = self._connection_generation

        # Retire an existing transport before opening a replacement. The
        # generation has already advanced, so any late events from the old
        # connection are harmlessly ignored.
        if old_conn is not None:
            with suppress(Exception):
                await old_conn.disconnect()

        self.protocol = ProtocolState()
        self._clear_bus_subscriptions()

        # ANSI state belongs to one transport stream and must never bleed
        # across reconnect generations.
        self.ansi = AnsiParser()

        bus = EventBus()
        self.bus = bus
        self._wire_bus(bus, generation)

        conn = MudConnection(
            host,
            port,
            bus,
            terminal_type=self.terminal_type,
        )
        self.conn = conn

        self._set_state(
            host=host,
            port=port,
            connected=False,
            connecting=True,
            reconnecting=False,
            status_text=f"connecting to {host}:{port}...",
        )

        try:
            await conn.connect()
        except OSError as exc:
            if self._is_current_generation(generation):
                self._system(f"[connection failed: {exc}]")
                self._set_state(
                    connected=False,
                    connecting=False,
                    status_text="disconnected",
                )
                self._schedule_reconnect()
            return

        if not self._is_current_generation(generation):
            with suppress(Exception):
                await conn.disconnect()
            return

        self._reconnect_attempt = 0

        self._report_automation_load_status()
        self._report_variable_load_status()

        if self._tick_task is None or self._tick_task.done():
            self._tick_task = self._task_owner.create(
                self._tick_loop(),
                name="mud-session-tick-loop",
            )

    async def disconnect(self, *, manual: bool = True) -> None:
        if manual:
            self._manual_disconnect = True
            self._cancel_reconnect_task()

        conn = self.conn
        if conn is None:
            self._set_state(
                connected=False,
                connecting=False,
                reconnecting=False,
                status_text="disconnected",
            )
            return

        await conn.disconnect()

        if manual:
            self._system("[disconnected by user -- use reconnect to reconnect]")

    async def reconnect(self) -> None:
        self._cancel_reconnect_task()
        self._reconnect_attempt = 0

        host = self.state.host
        port = self.state.port
        if not host or port is None:
            self._system("[no previous connection to reconnect to]")
            return

        self._manual_disconnect = True
        old_conn = self.conn
        if old_conn is not None:
            with suppress(Exception):
                await old_conn.disconnect()

        self._manual_disconnect = False
        if not self._shutting_down:
            await self.connect(host, port)

    async def close(self) -> None:
        """Shut the session down completely and await owned background tasks.

        Advancing the transport generation first invalidates any late EventBus
        callbacks from the retiring connection.  Without that step, a delayed
        DISCONNECTED/ERROR event could overwrite the terminal ``closed`` state
        after teardown had already completed.
        """
        self._shutting_down = True
        self._manual_disconnect = True
        self.mapper_walker.cancel("cancelled: session closed")
        self._connection_generation += 1

        reconnect_task = self._reconnect_task
        tick_task = self._tick_task
        self._reconnect_task = None
        self._tick_task = None

        # Invalidate callbacks and stop session background work before awaiting
        # transport teardown.  In particular, the tick loop must not fire
        # timers while a session is already shutting down.
        self._clear_bus_subscriptions()
        await self._task_owner.cancel_and_wait(
            extra=(reconnect_task, tick_task),
        )

        if self.conn is not None:
            with suppress(Exception):
                await self.conn.disconnect()
            self.conn = None
        with suppress(Exception):
            self.mapper.close()

        self._set_state(
            connected=False,
            connecting=False,
            reconnecting=False,
            status_text="closed",
        )

    # ------------------------------------------------------------------
    # Transport generation / EventBus
    # ------------------------------------------------------------------

    def _wire_bus(self, bus: EventBus, generation: int) -> None:
        bindings = (
            (EventType.TEXT, lambda event: self._on_incoming(generation, event.data, False)),
            (EventType.PROMPT, lambda event: self._on_incoming(generation, event.data, True)),
            (EventType.CONNECTED, lambda event: self._on_connected(generation, event.data)),
            (EventType.DISCONNECTED, lambda _event: self._on_disconnected(generation)),
            (EventType.GMCP, lambda event: self._on_gmcp(generation, event.data)),
            (EventType.MSDP, lambda event: self._on_msdp(generation, event.data)),
            (EventType.OPTION_CHANGE, lambda event: self._on_option_change(generation, event.data)),
            (EventType.PROTOCOL_NOTICE, lambda event: self._on_protocol_notice(generation, event.data)),
            (EventType.ERROR, lambda event: self._on_error(generation, event.data)),
        )
        self._bus_subscriptions.extend(
            bus.on(event_type, handler) for event_type, handler in bindings
        )

    def _clear_bus_subscriptions(self) -> None:
        subscriptions, self._bus_subscriptions = self._bus_subscriptions, []
        for subscription in subscriptions:
            subscription.close()

    def _is_current_generation(self, generation: int) -> bool:
        return generation == self._connection_generation

    def _on_connected(self, generation: int, data: Any) -> None:
        if not self._is_current_generation(generation):
            return
        host = data.get("host", self.state.host)
        port = data.get("port", self.state.port)
        self._set_state(
            host=host,
            port=port,
            connected=True,
            connecting=False,
            reconnecting=False,
            status_text=f"connected to {host}:{port}",
        )

    def _on_disconnected(self, generation: int) -> None:
        if not self._is_current_generation(generation):
            return

        flushed = self.ansi.flush_line()
        if flushed is not None:
            self._process_line(flushed)

        self._last_text_time = 0.0
        self.mapper_walker.cancel("stopped: disconnected")
        self._set_state(
            connected=False,
            connecting=False,
            reconnecting=False,
            status_text="disconnected",
        )
        self._system("[disconnected]")
        self._schedule_reconnect()

    def _on_error(self, generation: int, data: Any) -> None:
        if self._is_current_generation(generation):
            self._system(f"[error: {data}]")

    # ------------------------------------------------------------------
    # Reconnect policy
    # ------------------------------------------------------------------

    def _schedule_reconnect(self) -> None:
        if self._manual_disconnect or self._shutting_down or not self.auto_reconnect:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        if not self.state.host or self.state.port is None:
            return

        delay = min(
            self.reconnect_base_delay * (2 ** self._reconnect_attempt),
            self.reconnect_max_delay,
        )
        self._reconnect_attempt += 1
        self._set_state(
            reconnecting=True,
            status_text=f"reconnecting in {delay:.0f}s...",
        )
        self._system(f"[reconnecting in {delay:.0f}s...]")
        self._reconnect_task = self._task_owner.create(
            self._reconnect_after(delay),
            name="mud-session-reconnect",
        )

    async def _reconnect_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            if self._manual_disconnect or self._shutting_down:
                return
            if not self.state.host or self.state.port is None:
                return
            self._system(
                f"[reconnecting to {self.state.host}:{self.state.port}...]"
            )
            await self.connect(self.state.host, self.state.port)
        except asyncio.CancelledError:
            raise

    def _cancel_reconnect_task(self) -> None:
        task = self._reconnect_task
        self._reconnect_task = None
        if task is None or task.done():
            return

        # Automatic reconnect executes connect() *inside* the reconnect task.
        # connect() calls this helper to retire any older pending reconnect.
        # Cancelling the current task here would make the reconnect cancel
        # itself at its next await.
        if task is asyncio.current_task():
            return

        task.cancel()

    # ------------------------------------------------------------------
    # Incoming text / protocol state
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
        try:
            completed = self.ansi.feed(raw_text)
        except AnsiParseLimitError as exc:
            # A hostile/broken server can otherwise grow one never-terminated
            # logical line indefinitely even though transport chunks are
            # individually bounded.  Reset only presentation parsing; keep the
            # transport alive and make the event visible to the user.
            self.ansi.reset()
            self._last_text_time = 0.0
            self._system(f"[ANSI input discarded: {exc}]")
            return
        for line in completed:
            self._process_line(line)

        if is_prompt:
            flushed = self.ansi.flush_line()
            if flushed is not None:
                self._process_line(flushed)
            self._last_text_time = 0.0
            self.mapper_walker.note_prompt()

    def _process_line(self, line: StyledLine) -> None:
        self.last_styled_line = line
        # Preserve the original Textual ordering exactly:
        # scrollback -> automation -> gag decision -> visible presentation.
        self.scrollback.append(line)
        kept = self.engine.on_styled_text(line)
        if kept is not None:
            self._emit("line", line)

    def _check_prompt_idle_timeout(self) -> None:
        if self._last_text_time == 0.0:
            return
        if time.monotonic() - self._last_text_time < self.PROMPT_IDLE_TIMEOUT:
            return

        flushed = self.ansi.flush_line()
        self._last_text_time = 0.0
        if flushed is not None:
            self._process_line(flushed)
        # Idle flush is the fallback prompt boundary for MUDs that do not send
        # GA/EOR. It cannot advance a mapper step without a matching room ack.
        self.mapper_walker.note_prompt()

    def _bounded_protocol_set(self, mapping: dict, key: Any, value: Any) -> None:
        """Store protocol snapshot state without allowing unbounded key growth.

        Existing keys are updated in place.  A genuinely new key evicts the
        oldest snapshot entry once the configured cap is reached.  Protocol
        event delivery remains unaffected; this only bounds retained inspector
        state.
        """
        if key not in mapping and len(mapping) >= self.MAX_PROTOCOL_SNAPSHOT_ENTRIES:
            oldest = next(iter(mapping), None)
            if oldest is not None:
                mapping.pop(oldest, None)
        mapping[key] = value

    def _on_gmcp(self, generation: int, data: Any) -> None:
        if not self._is_current_generation(generation) or not isinstance(data, dict):
            return

        package = data.get("package")
        if isinstance(package, str):
            self._bounded_protocol_set(
                self.protocol.gmcp,
                package,
                data.get("data"),
            )
        self.protocol.record_event("GMCP", data)
        if self.mapper_adapter_supports(MapperAdapterCapability.GMCP):
            try:
                self._apply_mapper_observation(
                    self.mapper_adapter.observe_gmcp(str(package or ""), data.get("data"))
                )
            except Exception as exc:
                logger.warning("mapper adapter GMCP failure: %s", exc)
        self._emit("gmcp", data)

        # Preserve the old lightweight Char.Vitals status behavior without
        # making the generic session model depend on one MUD's package set.
        if package == "Char.Vitals" and isinstance(data.get("data"), dict):
            payload = data["data"]
            hp = payload.get("hp")
            maxhp = payload.get("maxhp")
            if hp is not None and maxhp is not None and self.state.connected:
                self._set_state(status_text=f"connected  |  HP {hp}/{maxhp}")

    def _on_msdp(self, generation: int, data: Any) -> None:
        if not self._is_current_generation(generation):
            return
        # client_core emits one MSDP variable at a time as
        # {"variable": <name>, "value": <decoded value>}.  Preserve a
        # convenient variable->value snapshot for the inspector.
        if isinstance(data, dict) and isinstance(data.get("variable"), str):
            self._bounded_protocol_set(
                self.protocol.msdp,
                data["variable"],
                data.get("value"),
            )
        elif isinstance(data, dict):
            # Compatibility for callers/tests that provide an already-flattened
            # mapping rather than the transport event envelope.
            for key, value in data.items():
                self._bounded_protocol_set(self.protocol.msdp, key, value)
        else:
            self._bounded_protocol_set(self.protocol.msdp, "value", data)
        self.protocol.record_event("MSDP", data)
        if self.mapper_adapter_supports(MapperAdapterCapability.MSDP):
            try:
                if isinstance(data, dict) and isinstance(data.get("variable"), str):
                    self._apply_mapper_observation(
                        self.mapper_adapter.observe_msdp(data["variable"], data.get("value"))
                    )
            except Exception as exc:
                logger.warning("mapper adapter MSDP failure: %s", exc)
        self._emit("msdp", data)

    def _on_option_change(self, generation: int, data: Any) -> None:
        if not self._is_current_generation(generation) or not isinstance(data, dict):
            return
        option = data.get("option")
        state = data.get("state")
        if isinstance(option, int) and isinstance(state, str):
            self.protocol.telnet_options[option] = state
        self.protocol.record_event("Telnet", data)
        self._emit("telnet", data)

    def _on_protocol_notice(self, generation: int, data: Any) -> None:
        """Record transport diagnostics without mutating negotiated state."""
        if not self._is_current_generation(generation):
            return
        self.protocol.record_event("Protocol", data)
        # The Qt bridge already refreshes the protocol dock on the generic
        # telnet signal. Reuse that notification without pretending this notice
        # is an OPTION_CHANGE.
        self._emit("telnet", data)

    # ------------------------------------------------------------------
    # Outbound commands / history
    # ------------------------------------------------------------------

    def submit_command(
        self,
        command: str,
        *,
        source: CommandSource = CommandSource.MANUAL,
    ) -> CommandResult:
        """Submit a command through the single outbound policy boundary."""
        return self.dispatch_command(CommandRequest(command, source))

    def dispatch_command(self, request: CommandRequest) -> CommandResult:
        return self.command_pipeline.dispatch(request)

    def _send_transport_line(self, line: str) -> None:
        conn = self.conn
        if conn is None:
            return
        conn.send_line(line)

    def _record_history(self, command: str) -> None:
        self._history.append(command)
        if len(self._history) > self.MAX_COMMAND_HISTORY:
            del self._history[: len(self._history) - self.MAX_COMMAND_HISTORY]
        self._history_pos = len(self._history)
        self._history_draft = ""

    @property
    def server_echo_enabled(self) -> bool:
        # Telnet option 1 is ECHO.  OPTION_CHANGE stores the remote WILL/WONT
        # state as lower-case command names.  Absence means the server has not
        # taken responsibility for echo, so local echo remains enabled.
        return self.protocol.telnet_options.get(1) == "will"

    def _local_echo(self, text: str) -> None:
        if not self.local_echo_enabled:
            return
        # Local command echo is presentation history, not incoming MUD text:
        # it belongs in scrollback but must not run incoming-text triggers.
        line = StyledLine(
            [Segment(text=text, style=Style(fg=(180, 210, 255)))]
        )
        self.scrollback.append(line)
        self._emit("line", line)

    def history_previous(self, current_draft: str | None = None) -> str | None:
        if not self._history:
            return None
        if self._history_pos >= len(self._history) and current_draft is not None:
            self._history_draft = current_draft
        self._history_pos = max(0, self._history_pos - 1)
        return self._history[self._history_pos]

    def history_next(self) -> str:
        if not self._history:
            return self._history_draft
        self._history_pos = min(len(self._history), self._history_pos + 1)
        if self._history_pos < len(self._history):
            return self._history[self._history_pos]
        return self._history_draft

    def set_scrollback_limit(self, max_lines: int) -> None:
        """Apply a new bounded scrollback limit without discarding newest lines."""
        self.scrollback.set_max_lines(max_lines)

    def _send_current_line(self, line: str) -> None:
        """Route trigger/timer output through the outbound command boundary."""
        self.dispatch_command(CommandRequest(line, CommandSource.AUTOMATION))

    # ------------------------------------------------------------------
    # Client-side commands (migrated from Textual app.py)
    # ------------------------------------------------------------------

    def _handle_client_command(self, body: str) -> None:
        parts = body.split(None, 1)
        cmd = parts[0].lower() if parts else ""
        rest = parts[1] if len(parts) > 1 else ""

        if cmd == "alias":
            if " = " not in rest:
                self._system("[usage: #alias PATTERN = EXPANSION]")
                return
            pattern, expansion = (part.strip() for part in rest.split(" = ", 1))
            self.engine.add_alias(pattern, expansion)
            self._system(f"[alias added: {pattern!r} -> {expansion!r}]")
            self._emit("automation", None)
            return

        if cmd == "unalias":
            pattern = rest.strip()
            removed = self._remove_by_pattern(self.engine.aliases, pattern)
            self._system(f"[removed {removed} alias(es) matching {pattern!r}]")
            self._emit("automation", None)
            return

        if cmd == "trigger":
            if " = " not in rest:
                self._system(
                    "[usage: #trigger PATTERN = RESPONSE "
                    "[:: gag oneshot cooldown=N]]"
                )
                return
            pattern, remainder = rest.split(" = ", 1)
            response, flags = (remainder.split(" :: ", 1) + [""])[:2]
            flag_tokens = flags.split()
            gag = "gag" in flag_tokens
            one_shot = "oneshot" in flag_tokens
            cooldown_s = 0.0
            for token in flag_tokens:
                if token.startswith("cooldown="):
                    try:
                        cooldown_s = float(token.split("=", 1)[1])
                    except ValueError:
                        self._system(f"[invalid cooldown value: {token!r}]")
                        return
            pattern = pattern.strip()
            response = response.strip()
            try:
                self.engine.add_simple_trigger(
                    pattern,
                    response,
                    gag=gag,
                    one_shot=one_shot,
                    cooldown_s=cooldown_s,
                )
            except (ValueError, re.error) as exc:
                self._system(f"[could not add trigger: {exc}]")
                return
            self._system(f"[trigger added: {pattern!r} -> {response!r}]")
            self._emit("automation", None)
            return

        if cmd == "untrigger":
            pattern = rest.strip()
            removed = self._remove_by_pattern(self.engine.triggers, pattern)
            self._system(f"[removed {removed} trigger(s) matching {pattern!r}]")
            self._emit("automation", None)
            return

        if cmd == "set":
            if " = " not in rest:
                self._system("[usage: #set NAME[:TYPE] = VALUE]")
                return
            lhs, raw_value = rest.split(" = ", 1)
            lhs = lhs.strip()
            if ":" in lhs:
                name, type_name = (part.strip() for part in lhs.rsplit(":", 1))
            else:
                name, type_name = lhs, "string"
            try:
                value = parse_typed_value(type_name, raw_value)
                self.set_variable(name, value)
            except (ValueError, OSError) as exc:
                self._system(f"[could not set variable: {exc}]")
                return
            self._system(f"[variable {name!r} set ({type_name.lower()})]")
            return

        if cmd == "unset":
            name = rest.strip()
            if not name:
                self._system("[usage: #unset NAME]")
                return
            try:
                removed = self.remove_variable(name)
            except (ValueError, OSError) as exc:
                self._system(f"[could not remove variable: {exc}]")
                return
            self._system(
                f"[variable {name!r} {'removed' if removed else 'not found'}]"
            )
            return

        if cmd == "vars":
            entries = self.variables.items()
            if not entries:
                self._system(f"[no variables defined in {self.variable_scope_label} scope]")
            else:
                self._system(f"[variables — {self.variable_scope_label}]")
                for entry in entries:
                    self._system(
                        f"  {entry.name} ({entry.type_name}) = {entry.value!r}"
                    )
            return

        if cmd == "savevars":
            try:
                self.save_variables_now()
            except (ValueError, OSError) as exc:
                self._system(f"[could not save variables: {exc}]")
                return
            self._system(f"[saved variables to {self.variables_path}]")
            return

        if cmd == "list":
            entries: list[str] = []
            entries.extend(
                f"  alias:   {alias.pattern!r} -> {alias.expansion!r}"
                for alias in self.engine.aliases.values()
                if isinstance(alias.expansion, str)
            )
            entries.extend(
                f"  trigger: {trigger.pattern!r} -> {trigger.response_template!r}"
                for trigger in self.engine.triggers.values()
                if trigger.response_template is not None
            )
            if not entries:
                self._system("[no aliases or triggers defined]")
            else:
                self._system("[aliases/triggers]")
                for entry in entries:
                    self._system(entry)
            return

        if cmd == "save":
            try:
                save_automation(self.engine, self.automation_path)
            except (ValueError, OSError) as exc:
                self._system(f"[could not save automation: {exc}]")
                return
            self._system(f"[saved aliases/triggers to {self.automation_path}]")
            return

        if cmd == "saveprofile":
            name = rest.strip()
            if not name:
                self._system("[usage: #saveprofile NAME]")
            elif not self.state.host or self.state.port is None:
                self._system("[no connection target -- nothing to save]")
            else:
                try:
                    save_profile(
                        name,
                        self.state.host,
                        self.state.port,
                        terminal_type=self.terminal_type,
                        auto_reconnect=self.auto_reconnect,
                        reconnect_base_delay=self.reconnect_base_delay,
                        reconnect_max_delay=self.reconnect_max_delay,
                    )
                except (ValueError, OSError) as exc:
                    self._system(f"[could not save profile: {exc}]")
                    return
                self._system(
                    f"[saved profile {name!r} -> "
                    f"{self.state.host}:{self.state.port}]"
                )
            return

        if cmd == "reconnect":
            self._task_owner.create(self.reconnect(), name="mud-session-manual-reconnect")
            return

        if cmd == "disconnect":
            self._task_owner.create(
                self.disconnect(manual=True),
                name="mud-session-manual-disconnect",
            )
            return

        if cmd in ("help", ""):
            self._system(
                "[commands: #alias P = E | #unalias P | "
                "#trigger P = R [:: gag oneshot cooldown=N] | "
                "#untrigger P | #list | #set NAME[:TYPE] = VALUE | "
                "#unset NAME | #vars | #savevars | #save | #saveprofile NAME | "
                "#reconnect | #disconnect]"
            )
            return

        self._system(f"[unknown command: #{cmd} -- try #help]")

    @staticmethod
    def _remove_by_pattern(store: dict, pattern: str) -> int:
        identifiers = [
            identifier
            for identifier, item in store.items()
            if item.pattern == pattern
        ]
        for identifier in identifiers:
            del store[identifier]
        return len(identifiers)


    def save_automation_now(self) -> None:
        """Persist the active session's persistence-safe automation entries."""
        save_automation(self.engine, self.automation_path)
        self._emit("automation", None)

    # ------------------------------------------------------------------
    # Utilities / tick loop
    # ------------------------------------------------------------------

    def _system(self, text: str) -> None:
        line = StyledLine(
            [Segment(text=text, style=Style(fg=(230, 190, 70), italic=True))]
        )
        self._emit("system", line)

    async def _tick_loop(self) -> None:
        try:
            while True:
                self.engine.tick()
                self._check_prompt_idle_timeout()
                self.mapper_walker.check_timeout()
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            raise
