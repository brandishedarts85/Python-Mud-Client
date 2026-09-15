"""Prompt-aware, acknowledgement-driven mapper route walking.

The walker is a small UI-neutral state machine. It never owns sockets, asyncio
Tasks, Qt objects, or SQLite. The session controller supplies routes, sends one
command at a time, and feeds trustworthy room/prompt acknowledgements back into
it.

Mapper 4 adds two deliberately bounded capabilities:
* an optional pre-command for doors/special exits, acknowledged by a prompt
  before the actual movement command is sent; and
* controlled rerouting from a *known observed room*, with a small reroute
  budget and a prompt synchronization barrier before a replacement route runs.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Sequence

from mapper import RouteStep

DEFAULT_STEP_TIMEOUT = 8.0
DEFAULT_MAX_REROUTES = 3


@dataclass(frozen=True)
class WalkStatus:
    active: bool = False
    step_index: int = 0
    step_count: int = 0
    destination_room_id: int | None = None
    pending_room_id: int | None = None
    message: str = "idle"
    phase: str = "idle"
    reroute_count: int = 0


class MapperWalker:
    """Advance a route only after trustworthy command-cycle acknowledgement.

    Movement requires both the expected room identity and a prompt boundary.
    A pre-command (for example ``open north``) requires a prompt before the
    movement command is issued. If movement reaches an unexpected *known* room,
    an optional controller-owned reroute callback may provide a replacement
    route; the walker still waits for the current command's prompt boundary
    before sending the first rerouted step.
    """

    def __init__(
        self,
        send_command: Callable[[str], bool],
        *,
        reroute: Callable[[int, int], Sequence[RouteStep] | None] | None = None,
        max_reroutes: int = DEFAULT_MAX_REROUTES,
        step_timeout: float = DEFAULT_STEP_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
        status_changed: Callable[[WalkStatus], None] | None = None,
    ) -> None:
        if step_timeout <= 0:
            raise ValueError("step timeout must be > 0")
        if max_reroutes < 0:
            raise ValueError("max reroutes must be >= 0")
        self._send_command = send_command
        self._reroute = reroute
        self._max_reroutes = int(max_reroutes)
        self._step_timeout = float(step_timeout)
        self._clock = clock
        self._status_changed = status_changed or (lambda _status: None)
        self._steps: tuple[RouteStep, ...] = ()
        self._destination_room_id: int | None = None
        self._index = 0
        self._source_room_id: int | None = None
        self._pending_room_id: int | None = None
        self._room_ack = False
        self._prompt_ack = False
        self._deadline: float | None = None
        self._phase = "idle"
        self._reroute_count = 0
        self._status = WalkStatus()

    @property
    def status(self) -> WalkStatus:
        return self._status

    @property
    def active(self) -> bool:
        return self._status.active

    @property
    def route_steps(self) -> tuple[RouteStep, ...]:
        """Read-only snapshot of the currently active route.

        This exists for status/visualization consumers only. It never grants
        permission to advance the walker or send commands.
        """
        return self._steps[self._index :] if self.active else ()

    def start(
        self,
        steps: Sequence[RouteStep],
        *,
        source_room_id: int,
        destination_room_id: int,
    ) -> WalkStatus:
        if self.active:
            raise RuntimeError("a mapper walk is already active")
        route = tuple(steps)
        destination_room_id = int(destination_room_id)
        if not route:
            self._set_status(
                WalkStatus(
                    False,
                    0,
                    0,
                    destination_room_id,
                    None,
                    "already at destination",
                    "idle",
                    0,
                )
            )
            return self._status
        if route[0].source_room_id != int(source_room_id):
            raise ValueError("route does not begin at the current room")
        if route[-1].destination_room_id != destination_room_id:
            raise ValueError("route does not end at the requested destination")
        self._steps = route
        self._destination_room_id = destination_room_id
        self._index = 0
        self._source_room_id = int(source_room_id)
        self._reroute_count = 0
        self._begin_current_step()
        return self._status

    def cancel(self, reason: str = "cancelled") -> WalkStatus:
        if not self.active:
            return self._status
        self._finish(reason)
        return self._status

    def note_room(self, room_id: int) -> WalkStatus:
        if not self.active:
            return self._status
        room_id = int(room_id)

        if self._phase == "moving":
            if room_id == self._pending_room_id:
                self._room_ack = True
                self._advance_if_acknowledged()
                return self._status
            if room_id == self._source_room_id:
                return self._status
            self._handle_divergence(room_id)
            return self._status

        if self._phase == "preparing":
            # Opening/manipulating a door should not itself move the player.
            if room_id != self._source_room_id:
                self._handle_divergence(room_id)
            return self._status

        if self._phase == "reroute_wait":
            # We already accepted one unexpected room and are waiting only for
            # the prompt that closes that command cycle. A second different
            # room before that boundary is ambiguous, so stop rather than guess.
            if room_id != self._source_room_id:
                self._finish(
                    f"stopped: room changed again while synchronizing reroute ({room_id})"
                )
            return self._status

        return self._status

    def note_prompt(self) -> WalkStatus:
        if not self.active:
            return self._status

        if self._phase == "preparing":
            self._begin_movement_command()
            return self._status

        if self._phase == "moving":
            self._prompt_ack = True
            self._advance_if_acknowledged()
            return self._status

        if self._phase == "reroute_wait":
            self._prompt_ack = True
            self._activate_reroute_if_synchronized()
            return self._status

        return self._status

    def check_timeout(self) -> WalkStatus:
        if not self.active or self._deadline is None or self._clock() < self._deadline:
            return self._status
        if self._phase == "preparing":
            self._finish(
                f"stopped: pre-command acknowledgement timed out at step {self._index + 1}"
            )
        elif self._phase == "reroute_wait":
            self._finish("stopped: reroute synchronization timed out")
        else:
            self._finish(
                f"stopped: movement acknowledgement timed out at step {self._index + 1}"
            )
        return self._status

    def _begin_current_step(self) -> None:
        step = self._steps[self._index]
        self._source_room_id = step.source_room_id
        self._pending_room_id = step.destination_room_id
        self._room_ack = False
        self._prompt_ack = False
        self._deadline = self._clock() + self._step_timeout

        if step.pre_command:
            self._phase = "preparing"
            self._set_status(
                WalkStatus(
                    True,
                    self._index,
                    len(self._steps),
                    self._destination_room_id,
                    self._pending_room_id,
                    f"walking: step {self._index + 1}/{len(self._steps)} prepare ({step.pre_command})",
                    self._phase,
                    self._reroute_count,
                )
            )
            if not self._send_command(step.pre_command):
                self._finish("stopped: exit pre-command was not sent")
            return

        self._begin_movement_command()

    def _begin_movement_command(self) -> None:
        step = self._steps[self._index]
        self._phase = "moving"
        self._room_ack = False
        self._prompt_ack = False
        self._deadline = self._clock() + self._step_timeout
        label = step.direction
        if step.kind != "normal":
            label = f"{step.direction}, {step.kind}"
        self._set_status(
            WalkStatus(
                True,
                self._index,
                len(self._steps),
                self._destination_room_id,
                self._pending_room_id,
                f"walking: step {self._index + 1}/{len(self._steps)} ({label})",
                self._phase,
                self._reroute_count,
            )
        )
        if not self._send_command(step.command):
            self._finish("stopped: movement command was not sent")

    def _advance_if_acknowledged(self) -> None:
        if not (
            self.active
            and self._phase == "moving"
            and self._room_ack
            and self._prompt_ack
        ):
            return
        self._index += 1
        if self._index >= len(self._steps):
            self._finish("arrived")
            return
        self._source_room_id = self._pending_room_id
        self._begin_current_step()

    def _handle_divergence(self, room_id: int) -> None:
        expected = self._pending_room_id
        destination = self._destination_room_id
        if destination is None:
            self._finish(f"stopped: route diverged at room {room_id}")
            return

        if self._reroute is None or self._reroute_count >= self._max_reroutes:
            suffix = "reroute limit reached" if self._reroute is not None else "rerouting disabled"
            self._finish(
                f"stopped: route diverged at room {room_id}; expected {expected} ({suffix})"
            )
            return

        if room_id == destination:
            route: tuple[RouteStep, ...] = ()
        else:
            try:
                replacement = self._reroute(room_id, destination)
            except Exception:
                replacement = None
            if replacement is None:
                self._finish(
                    f"stopped: route diverged at room {room_id}; expected {expected}; no safe reroute"
                )
                return
            route = tuple(replacement)
            if (
                not route
                or route[0].source_room_id != room_id
                or route[-1].destination_room_id != destination
            ):
                self._finish(
                    f"stopped: route diverged at room {room_id}; replacement route was invalid"
                )
                return

        self._reroute_count += 1
        self._steps = route
        self._index = 0
        self._source_room_id = room_id
        self._pending_room_id = None
        self._room_ack = True
        # Preserve a prompt that may have arrived before the room observation.
        self._phase = "reroute_wait"
        self._deadline = self._clock() + self._step_timeout
        self._set_status(
            WalkStatus(
                True,
                0,
                len(route),
                destination,
                None,
                (
                    f"rerouting from room {room_id} ({self._reroute_count}/{self._max_reroutes})"
                    if route
                    else "destination reached by divergent movement; synchronizing prompt"
                ),
                self._phase,
                self._reroute_count,
            )
        )
        self._activate_reroute_if_synchronized()

    def _activate_reroute_if_synchronized(self) -> None:
        if not (
            self.active
            and self._phase == "reroute_wait"
            and self._room_ack
            and self._prompt_ack
        ):
            return
        if not self._steps:
            self._finish("arrived")
            return
        self._begin_current_step()

    def _finish(self, message: str) -> None:
        completed = self._index
        if self._phase == "moving" and self._room_ack and self._prompt_ack:
            completed += 1
        completed = min(self._status.step_count, completed)
        self._deadline = None
        self._pending_room_id = None
        self._steps = ()
        self._room_ack = False
        self._prompt_ack = False
        self._phase = "idle"
        self._set_status(
            WalkStatus(
                False,
                completed,
                self._status.step_count,
                self._destination_room_id,
                None,
                message,
                "idle",
                self._reroute_count,
            )
        )

    def _set_status(self, status: WalkStatus) -> None:
        self._status = status
        self._status_changed(status)
