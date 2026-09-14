"""
automation.py -- the automation engine.

Triggers, aliases, timers, and lightweight plugin hooks driven by the
semantic text/command boundaries produced by the rest of the client.

The normal application pipeline is:

    client_core
        -> ansi_parser
        -> complete plain-text line
        -> AutomationEngine
        -> UI render / gag decision

Automation deliberately does not know about sockets or widgets. It sends
commands only through the SendFn callback supplied by the application.

Contains:
  - Alias:
      typed command pattern -> expansion
  - Trigger:
      regex against one complete incoming text line -> action
  - Timer:
      one-shot or repeating monotonic-clock callback
  - AutomationEngine:
      owns aliases, triggers, timers, and event hooks

Simple aliases/triggers use strings and can be persisted safely.
Code-defined callables remain application/plugin-owned runtime behavior.
"""

from __future__ import annotations

import logging
import math
import re
import time
import uuid

from dataclasses import dataclass, field
from typing import Callable, Optional


logger = logging.getLogger("mudclient.automation")


SendFn = Callable[[str], None]
EventHook = Callable[[str], None]


try:
    # Only required for the optional legacy/convenience attach() helper.
    from client_core import EventBus, EventType

except ImportError:  # pragma: no cover - standalone unit testing
    EventBus = None  # type: ignore[assignment]
    EventType = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_nonnegative_seconds(
    value: float,
    *,
    name: str,
) -> float:
    """
    Validate a duration which may legally be zero.

    Reject NaN/infinity as well as negative values. Those values otherwise
    produce extremely confusing timer/cooldown behavior.
    """

    value = float(value)

    if not math.isfinite(value):
        raise ValueError(
            f"{name} must be finite"
        )

    if value < 0:
        raise ValueError(
            f"{name} must be >= 0"
        )

    return value


def _validate_positive_seconds(
    value: float,
    *,
    name: str,
) -> float:
    """
    Validate a strictly-positive duration.

    Repeating zero-second timers would effectively become "fire every UI
    tick", which is almost never intentional and can create avoidable load.
    """

    value = float(value)

    if not math.isfinite(value):
        raise ValueError(
            f"{name} must be finite"
        )

    if value <= 0:
        raise ValueError(
            f"{name} must be > 0"
        )

    return value


def _substitute_numbered_groups(
    template: str,
    groups: tuple[str | None, ...],
) -> str:
    """
    Replace classic MUD-client %1..%9 capture references.

    The syntax intentionally stops at %9. Limiting substitution to the
    documented range also prevents replacing "%1" inside an accidental "%10".
    """

    text = template

    upper = min(
        len(groups),
        9,
    )

    # Work backwards so the behavior remains unambiguous if this syntax is
    # ever expanded in the future.
    for index in range(
        upper,
        0,
        -1,
    ):
        value = groups[index - 1] or ""

        text = text.replace(
            f"%{index}",
            value,
        )

    return text


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------


@dataclass
class Alias:
    id: str

    # Examples:
    #   "kk"
    #   "gt *"
    #
    # "*" captures arbitrary text at that position.
    pattern: str

    expansion: (
        str
        | Callable[[list[str]], str]
    )

    enabled: bool = True

    _regex: re.Pattern[str] = field(
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        escaped = re.escape(
            self.pattern
        )

        # Classic MUD-client wildcard capture.
        escaped = escaped.replace(
            r"\*",
            "(.*)",
        )

        self._regex = re.compile(
            rf"^{escaped}$",
            re.IGNORECASE,
        )

    def matches(
        self,
        command: str,
    ) -> Optional[re.Match[str]]:
        if not self.enabled:
            return None

        return self._regex.match(
            command
        )

    def expand(
        self,
        match: re.Match[str],
    ) -> str:
        groups = list(
            match.groups()
        )

        if callable(self.expansion):
            return self.expansion(
                groups
            )

        return _substitute_numbered_groups(
            self.expansion,
            match.groups(),
        )


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------


@dataclass
class Trigger:
    id: str

    # Python regular expression matched against one complete incoming line.
    pattern: str

    action: Callable[
        [re.Match[str], "TriggerContext"],
        None,
    ]

    enabled: bool = True

    # Hide line from visible presentation after trigger processing.
    gag: bool = False

    # Disable after the first match/fire attempt.
    one_shot: bool = False

    # Minimum monotonic seconds between firings.
    cooldown_s: float = 0.0

    case_sensitive: bool = False

    # Present only for persistence-safe template triggers.
    # None means this is a code-defined runtime action.
    response_template: Optional[str] = None

    _regex: re.Pattern[str] = field(
        init=False,
        repr=False,
    )

    _last_fired: Optional[float] = field(
        default=None,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        self.cooldown_s = _validate_nonnegative_seconds(
            self.cooldown_s,
            name="cooldown_s",
        )

        flags = (
            0
            if self.case_sensitive
            else re.IGNORECASE
        )

        self._regex = re.compile(
            self.pattern,
            flags,
        )

    def check(
        self,
        line: str,
        *,
        now: Optional[float] = None,
    ) -> Optional[re.Match[str]]:
        if not self.enabled:
            return None

        if now is None:
            now = time.monotonic()

        if (
            self.cooldown_s > 0
            and self._last_fired is not None
            and (
                now
                - self._last_fired
            )
            < self.cooldown_s
        ):
            return None

        return self._regex.search(
            line
        )

    def fire(
        self,
        match: re.Match[str],
        ctx: "TriggerContext",
        *,
        now: Optional[float] = None,
    ) -> None:
        if now is None:
            now = time.monotonic()

        # A firing attempt consumes cooldown even if user/plugin code raises.
        # Otherwise a broken action could be retried continuously on every
        # matching line.
        self._last_fired = now

        try:
            self.action(
                match,
                ctx,
            )

        except Exception:
            logger.exception(
                "trigger %s action raised",
                self.id,
            )

        finally:
            if self.one_shot:
                self.enabled = False


@dataclass
class TriggerContext:
    """
    Context supplied to trigger actions.

    This deliberately exposes a narrow interface rather than requiring the
    action to know about the UI or MudConnection.
    """

    line: str
    send: SendFn
    engine: "AutomationEngine"


# ---------------------------------------------------------------------------
# Timers
# ---------------------------------------------------------------------------


@dataclass
class Timer:
    id: str
    interval_s: float
    action: Callable[[], None]

    repeat: bool = True
    enabled: bool = True

    _next_fire: float = field(
        default=0.0,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        self.interval_s = _validate_positive_seconds(
            self.interval_s,
            name="interval_s",
        )

        self._next_fire = (
            time.monotonic()
            + self.interval_s
        )

    def due(
        self,
        now: float,
    ) -> bool:
        return (
            self.enabled
            and now >= self._next_fire
        )

    def fire(
        self,
        *,
        now: Optional[float] = None,
    ) -> None:
        if not self.enabled:
            return

        if now is None:
            now = time.monotonic()

        scheduled_fire = self._next_fire

        try:
            self.action()

        except Exception:
            logger.exception(
                "timer %s action raised",
                self.id,
            )

        if not self.repeat:
            self.enabled = False
            return

        # Preserve timer cadence without performing a catch-up burst.
        #
        # Example:
        #
        #   interval = 5 seconds
        #   UI stalls for 22 seconds
        #
        # We fire once, then advance to the first future scheduled boundary.
        # We do NOT fire four missed callbacks in one tick.
        after_action = time.monotonic()

        if after_action < scheduled_fire:
            self._next_fire = (
                scheduled_fire
                + self.interval_s
            )
            return

        elapsed = (
            after_action
            - scheduled_fire
        )

        periods = (
            int(
                elapsed
                // self.interval_s
            )
            + 1
        )

        self._next_fire = (
            scheduled_fire
            + periods
            * self.interval_s
        )

    def restart(self) -> None:
        """
        Restart this timer's interval from now.

        Useful when re-enabling a timer and the caller does not want an old
        overdue schedule to fire immediately.
        """

        self._next_fire = (
            time.monotonic()
            + self.interval_s
        )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class AutomationEngine:
    """
    Owns triggers, aliases, timers, and text hooks.

    Normal client usage:

        engine = AutomationEngine(send_fn=app_send_adapter)

        # Complete ANSI-stripped lines:
        kept = engine.on_text(line)

        # User command:
        commands = engine.on_command(command)

        # Periodic UI tick:
        engine.tick()

    The application owns transport and presentation.

    Important:
        on_text() expects a COMPLETE SEMANTIC LINE.

    It should normally be called after ANSI parsing / prompt framing rather
    than directly from arbitrary socket TEXT chunks.
    """

    _VALID_KINDS = (
        "trigger",
        "alias",
        "timer",
    )

    def __init__(
        self,
        send_fn: SendFn,
    ) -> None:
        self.send = send_fn

        self.triggers: dict[
            str,
            Trigger,
        ] = {}

        self.aliases: dict[
            str,
            Alias,
        ] = {}

        self.timers: dict[
            str,
            Timer,
        ] = {}

        self._event_hooks: list[
            EventHook
        ] = []

        # attach() exists for compatibility/convenience. Track buses so
        # accidental repeated attachment does not duplicate event delivery.
        self._attached_bus_ids: set[int] = set()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def add_trigger(
        self,
        pattern: str,
        action: Callable[
            [re.Match[str], TriggerContext],
            None,
        ],
        *,
        gag: bool = False,
        one_shot: bool = False,
        cooldown_s: float = 0.0,
        case_sensitive: bool = False,
        trigger_id: Optional[str] = None,
    ) -> str:
        cooldown_s = _validate_nonnegative_seconds(
            cooldown_s,
            name="cooldown_s",
        )

        trigger_id = (
            trigger_id
            or str(
                uuid.uuid4()
            )
        )

        self.triggers[
            trigger_id
        ] = Trigger(
            id=trigger_id,
            pattern=pattern,
            action=action,
            gag=gag,
            one_shot=one_shot,
            cooldown_s=cooldown_s,
            case_sensitive=case_sensitive,
        )

        return trigger_id

    def add_simple_trigger(
        self,
        pattern: str,
        response: str,
        *,
        gag: bool = False,
        one_shot: bool = False,
        cooldown_s: float = 0.0,
        case_sensitive: bool = False,
        trigger_id: Optional[str] = None,
    ) -> str:
        """
        Create a persistence-safe trigger whose action is simply:

            match
              -> substitute %1..%9
              -> split on ;;
              -> send commands

        Arbitrary Python callables are intentionally not represented by
        response_template and therefore should not be serialized by the normal
        persistence layer.
        """

        cooldown_s = _validate_nonnegative_seconds(
            cooldown_s,
            name="cooldown_s",
        )

        def action(
            match: re.Match[str],
            ctx: TriggerContext,
        ) -> None:
            text = _substitute_numbered_groups(
                response,
                match.groups(),
            )

            for command in text.split(
                ";;"
            ):
                command = command.strip()

                if command:
                    ctx.send(
                        command
                    )

        trigger_id = (
            trigger_id
            or str(
                uuid.uuid4()
            )
        )

        self.triggers[
            trigger_id
        ] = Trigger(
            id=trigger_id,
            pattern=pattern,
            action=action,
            gag=gag,
            one_shot=one_shot,
            cooldown_s=cooldown_s,
            case_sensitive=case_sensitive,
            response_template=response,
        )

        return trigger_id

    def add_alias(
        self,
        pattern: str,
        expansion: (
            str
            | Callable[
                [list[str]],
                str,
            ]
        ),
        *,
        alias_id: Optional[str] = None,
    ) -> str:
        alias_id = (
            alias_id
            or str(
                uuid.uuid4()
            )
        )

        self.aliases[
            alias_id
        ] = Alias(
            id=alias_id,
            pattern=pattern,
            expansion=expansion,
        )

        return alias_id

    def add_timer(
        self,
        interval_s: float,
        action: Callable[[], None],
        *,
        repeat: bool = True,
        timer_id: Optional[str] = None,
    ) -> str:
        interval_s = _validate_positive_seconds(
            interval_s,
            name="interval_s",
        )

        timer_id = (
            timer_id
            or str(
                uuid.uuid4()
            )
        )

        self.timers[
            timer_id
        ] = Timer(
            id=timer_id,
            interval_s=interval_s,
            action=action,
            repeat=repeat,
        )

        return timer_id

    def add_event_hook(
        self,
        hook: EventHook,
    ) -> None:
        """
        Register a hook that receives every complete incoming text line after
        trigger processing.

        Registering the same callable twice is ignored.
        """

        if hook not in self._event_hooks:
            self._event_hooks.append(
                hook
            )

    def remove_event_hook(
        self,
        hook: EventHook,
    ) -> bool:
        """
        Remove an event hook.

        Returns True when the hook existed.
        """

        try:
            self._event_hooks.remove(
                hook
            )

        except ValueError:
            return False

        return True

    # ------------------------------------------------------------------
    # Enable / disable / removal
    # ------------------------------------------------------------------

    def _store_for_kind(
        self,
        kind: str,
    ):
        if kind == "trigger":
            return self.triggers

        if kind == "alias":
            return self.aliases

        if kind == "timer":
            return self.timers

        valid = ", ".join(
            self._VALID_KINDS
        )

        raise ValueError(
            f"unknown automation kind {kind!r}; "
            f"expected one of: {valid}"
        )

    def set_enabled(
        self,
        kind: str,
        id_: str,
        enabled: bool,
    ) -> bool:
        """
        Enable or disable an automation entry.

        Returns False if the requested ID does not exist.

        Re-enabling a timer restarts its interval from now rather than causing
        an old overdue schedule to fire immediately.
        """

        store = self._store_for_kind(
            kind
        )

        item = store.get(
            id_
        )

        if item is None:
            return False

        was_enabled = item.enabled

        item.enabled = bool(
            enabled
        )

        if (
            kind == "timer"
            and item.enabled
            and not was_enabled
        ):
            item.restart()

        return True

    def remove(
        self,
        kind: str,
        id_: str,
    ) -> bool:
        """
        Remove one automation entry.

        Returns True when something was removed.
        """

        store = self._store_for_kind(
            kind
        )

        return (
            store.pop(
                id_,
                None,
            )
            is not None
        )

    # ------------------------------------------------------------------
    # Incoming text
    # ------------------------------------------------------------------

    def on_text(
        self,
        line: str,
    ) -> Optional[str]:
        """
        Process one complete plain-text game line.

        Returns:
            original line
                when it should remain visible

            None
                when one or more matching triggers gag it

        Trigger actions run before event hooks.

        A snapshot is used so actions/hooks may modify registration safely
        without invalidating the active iteration.
        """

        display_line: Optional[
            str
        ] = line

        ctx = TriggerContext(
            line=line,
            send=self.send,
            engine=self,
        )

        now = time.monotonic()

        for trigger in tuple(
            self.triggers.values()
        ):
            match = trigger.check(
                line,
                now=now,
            )

            if match is None:
                continue

            trigger.fire(
                match,
                ctx,
                now=now,
            )

            if trigger.gag:
                display_line = None

        for hook in tuple(
            self._event_hooks
        ):
            try:
                hook(
                    line
                )

            except Exception:
                logger.exception(
                    "event hook raised"
                )

        return display_line

    # ------------------------------------------------------------------
    # Outgoing commands
    # ------------------------------------------------------------------

    def on_command(
        self,
        command: str,
    ) -> list[str]:
        """
        Expand the first enabled matching alias.

        A single alias may expand into multiple commands separated by ";;".

        If a code-defined alias callable raises, the failure is logged and the
        command is suppressed rather than crashing the UI or accidentally
        sending the unresolved alias to the game.
        """

        for alias in tuple(
            self.aliases.values()
        ):
            match = alias.matches(
                command
            )

            if match is None:
                continue

            try:
                expanded = alias.expand(
                    match
                )

            except Exception:
                logger.exception(
                    "alias %s expansion raised",
                    alias.id,
                )

                return []

            if not isinstance(
                expanded,
                str,
            ):
                logger.error(
                    "alias %s returned non-string expansion %r",
                    alias.id,
                    type(expanded).__name__,
                )

                return []

            return [
                piece.strip()
                for piece in expanded.split(
                    ";;"
                )
                if piece.strip()
            ]

        return [
            command
        ]

    # ------------------------------------------------------------------
    # Timers
    # ------------------------------------------------------------------

    def tick(self) -> None:
        """
        Fire timers that are currently due.

        Each timer can fire at most once per tick.

        Repeating timers skip missed periods rather than producing a burst of
        catch-up actions after an event-loop stall.
        """

        now = time.monotonic()

        for timer in tuple(
            self.timers.values()
        ):
            if timer.due(
                now
            ):
                timer.fire(
                    now=now
                )

    # ------------------------------------------------------------------
    # Optional EventBus convenience wiring
    # ------------------------------------------------------------------

    def attach(
        self,
        bus: "EventBus",
    ) -> None:
        """
        Convenience/legacy EventBus wiring.

        IMPORTANT:

        The normal Textual application should NOT use this helper.

        MudConnection TEXT events may represent arbitrary transport chunks
        rather than complete rendered lines. The preferred application path is:

            EventBus
                -> AnsiParser
                -> complete StyledLine
                -> plain_text()
                -> AutomationEngine.on_text()

        attach() remains available for tests or alternate callers whose bus is
        known to emit complete clean text records.

        Calling attach() twice for the same EventBus is idempotent.
        """

        if (
            EventBus is None
            or EventType is None
        ):
            raise RuntimeError(
                "client_core.EventBus not importable"
            )

        bus_identity = id(
            bus
        )

        if (
            bus_identity
            in self._attached_bus_ids
        ):
            return

        self._attached_bus_ids.add(
            bus_identity
        )

        bus.on(
            EventType.TEXT,
            lambda event: self.on_text(
                event.data
            ),
        )

        bus.on(
            EventType.PROMPT,
            lambda event: self.on_text(
                event.data
            ),
        )
