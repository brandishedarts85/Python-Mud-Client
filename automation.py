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
from typing import Any, Callable, Optional

from ansi_parser import Style, StyledLine


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


RGB = tuple[int, int, int]


@dataclass(frozen=True)
class TriggerStyleFilter:
    """Optional visual-style constraints for an incoming-text trigger.

    Colors are stored as normalized RGB triples so the same matcher works for
    classic ANSI 16-color, xterm-256, and truecolor output after parsing.

    A None attribute means "ignore this attribute".  Foreground/background
    tuples are OR-lists: an empty tuple means "ignore this color channel".
    """

    foregrounds: tuple[RGB, ...] = ()
    backgrounds: tuple[RGB, ...] = ()
    allow_default_foreground: bool = False
    allow_default_background: bool = False
    bold: Optional[bool] = None
    dim: Optional[bool] = None
    italic: Optional[bool] = None
    underline: Optional[bool] = None
    blink: Optional[bool] = None
    strike: Optional[bool] = None

    def __post_init__(self) -> None:
        for name, colors in (("foregrounds", self.foregrounds), ("backgrounds", self.backgrounds)):
            normalized: list[RGB] = []
            for color in colors:
                if len(color) != 3 or any(not isinstance(v, int) or not 0 <= v <= 255 for v in color):
                    raise ValueError(f"{name}: colors must be RGB triples in the range 0..255")
                rgb = (int(color[0]), int(color[1]), int(color[2]))
                if rgb not in normalized:
                    normalized.append(rgb)
            object.__setattr__(self, name, tuple(normalized))

    @property
    def active(self) -> bool:
        return bool(
            self.foregrounds
            or self.backgrounds
            or self.allow_default_foreground
            or self.allow_default_background
            or any(
                value is not None
                for value in (
                    self.bold, self.dim, self.italic, self.underline,
                    self.blink, self.strike,
                )
            )
        )

    def matches_style(self, style: Style) -> bool:
        # Match the colors the player actually sees.  ANSI reverse swaps the
        # semantic foreground/background at presentation time.
        fg, bg = style.fg, style.bg
        if style.reverse:
            fg, bg = bg, fg

        foreground_restricted = bool(self.foregrounds or self.allow_default_foreground)
        if foreground_restricted:
            if fg is None:
                if not self.allow_default_foreground:
                    return False
            elif fg not in self.foregrounds:
                return False

        background_restricted = bool(self.backgrounds or self.allow_default_background)
        if background_restricted:
            if bg is None:
                if not self.allow_default_background:
                    return False
            elif bg not in self.backgrounds:
                return False

        for name in ("bold", "dim", "italic", "underline", "blink", "strike"):
            expected = getattr(self, name)
            if expected is not None and bool(getattr(style, name)) != expected:
                return False
        return True

    def matches_span(self, line: StyledLine, start: int, end: int) -> bool:
        """Return True when every non-whitespace character in a match span
        satisfies the filter.

        Requiring the style across the actual regex match prevents a chat line
        containing the same words in another color from spoofing a color-aware
        trigger.  Whitespace is ignored because many MUDs reset style around
        padding/indentation.
        """

        if not self.active:
            return True
        if start >= end:
            return False

        offset = 0
        saw_visible = False
        for segment in line.segments:
            seg_start = offset
            seg_end = offset + len(segment.text)
            offset = seg_end
            overlap_start = max(start, seg_start)
            overlap_end = min(end, seg_end)
            if overlap_start >= overlap_end:
                continue
            text = segment.text[overlap_start - seg_start:overlap_end - seg_start]
            if not any(not ch.isspace() for ch in text):
                continue
            saw_visible = True
            if not self.matches_style(segment.style):
                return False
        return saw_visible


@dataclass(frozen=True)
class TriggerFireEvent:
    trigger_id: str
    pattern: str
    line: str
    matched_text: str
    response: Optional[str]
    fired_at: float


@dataclass(frozen=True)
class TriggerTestResult:
    trigger_id: str
    pattern: str
    master_enabled: bool
    trigger_enabled: bool
    regex_matched: bool
    style_required: bool
    style_available: bool
    style_matched: Optional[bool]
    cooldown_ready: bool
    would_fire: bool
    matched_text: str = ""
    reason: str = ""


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

    # Higher priority triggers are evaluated first. Equal priorities preserve
    # insertion order, which keeps existing automation deterministic.
    priority: int = 0

    # Present only for persistence-safe template triggers.
    # None means this is a code-defined runtime action.
    response_template: Optional[str] = None

    # Optional parsed-ANSI constraints for this trigger.
    style_filter: Optional[TriggerStyleFilter] = None

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
        styled_line: Optional[StyledLine] = None,
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

        match = self._regex.search(line)
        if match is None:
            return None

        if self.style_filter is not None and self.style_filter.active:
            if styled_line is None:
                return None
            if not self.style_filter.matches_span(styled_line, match.start(), match.end()):
                return None

        return match

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
    styled_line: Optional[StyledLine] = None


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
        self.enabled = True

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

        self._trigger_fire_hooks: list[Callable[[TriggerFireEvent], None]] = []

        # attach() exists for compatibility/convenience. Track buses so
        # accidental repeated attachment does not duplicate event delivery.
        self._attached_bus_ids: set[int] = set()
        self._attached_bus_subscriptions: dict[int, list[Any]] = {}

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
        priority: int = 0,
        style_filter: Optional[TriggerStyleFilter] = None,
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
            priority=int(priority),
            style_filter=style_filter,
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
        priority: int = 0,
        style_filter: Optional[TriggerStyleFilter] = None,
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
            priority=int(priority),
            response_template=response,
            style_filter=style_filter,
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

    def add_trigger_fire_hook(
        self,
        hook: Callable[[TriggerFireEvent], None],
    ) -> None:
        if hook not in self._trigger_fire_hooks:
            self._trigger_fire_hooks.append(hook)

    def remove_trigger_fire_hook(
        self,
        hook: Callable[[TriggerFireEvent], None],
    ) -> bool:
        try:
            self._trigger_fire_hooks.remove(hook)
        except ValueError:
            return False
        return True

    def set_master_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)

    def explain_trigger(
        self,
        trigger_id: str,
        line: str,
        *,
        styled_line: Optional[StyledLine] = None,
        now: Optional[float] = None,
    ) -> TriggerTestResult:
        """Evaluate a trigger without firing it and explain the decision."""
        trigger = self.triggers.get(trigger_id)
        if trigger is None:
            raise KeyError(trigger_id)
        if now is None:
            now = time.monotonic()

        if not self.enabled:
            return TriggerTestResult(
                trigger_id, trigger.pattern, False, trigger.enabled, False,
                bool(trigger.style_filter and trigger.style_filter.active),
                styled_line is not None, None, True, False, reason="Master automation is disabled."
            )
        if not trigger.enabled:
            return TriggerTestResult(
                trigger_id, trigger.pattern, True, False, False,
                bool(trigger.style_filter and trigger.style_filter.active),
                styled_line is not None, None, True, False, reason="Trigger is disabled."
            )

        cooldown_ready = not (
            trigger.cooldown_s > 0
            and trigger._last_fired is not None
            and (now - trigger._last_fired) < trigger.cooldown_s
        )
        match = trigger._regex.search(line)
        style_required = bool(trigger.style_filter and trigger.style_filter.active)
        style_available = styled_line is not None
        style_matched: Optional[bool] = None
        matched_text = match.group(0) if match is not None else ""

        if match is None:
            reason = "Regex did not match."
            would_fire = False
        elif style_required and styled_line is None:
            reason = "Text matched, but ANSI/style evidence is unavailable."
            would_fire = False
        else:
            if style_required:
                assert trigger.style_filter is not None and styled_line is not None
                style_matched = trigger.style_filter.matches_span(
                    styled_line, match.start(), match.end()
                )
            if style_required and not style_matched:
                reason = "Text matched, but the captured ANSI/style constraints did not."
                would_fire = False
            elif not cooldown_ready:
                reason = "Text/style matched, but the trigger is still in cooldown."
                would_fire = False
            else:
                reason = "Trigger would fire."
                would_fire = True

        return TriggerTestResult(
            trigger_id=trigger_id,
            pattern=trigger.pattern,
            master_enabled=True,
            trigger_enabled=True,
            regex_matched=match is not None,
            style_required=style_required,
            style_available=style_available,
            style_matched=style_matched,
            cooldown_ready=cooldown_ready,
            would_fire=would_fire,
            matched_text=matched_text,
            reason=reason,
        )

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
        """Process one complete plain-text game line.

        Style-aware triggers intentionally do not fire through this legacy
        plain-text entry point because no ANSI style evidence is available.
        Use on_styled_text() when parsed presentation data exists.
        """
        return self._process_incoming(line, styled_line=None)

    def on_styled_text(
        self,
        line: StyledLine,
    ) -> Optional[str]:
        """Process one complete parsed ANSI line, including style filters."""
        return self._process_incoming(line.plain_text(), styled_line=line)

    def _process_incoming(
        self,
        line: str,
        *,
        styled_line: Optional[StyledLine],
    ) -> Optional[str]:
        display_line: Optional[str] = line

        ctx = TriggerContext(
            line=line,
            send=self.send,
            engine=self,
            styled_line=styled_line,
        )

        now = time.monotonic()

        if self.enabled:
            ordered_triggers = sorted(
                tuple(self.triggers.values()),
                key=lambda trigger: -trigger.priority,
            )
            for trigger in ordered_triggers:
                match = trigger.check(
                    line,
                    styled_line=styled_line,
                    now=now,
                )
                if match is None:
                    continue

                trigger.fire(match, ctx, now=now)
                response = None
                if trigger.response_template is not None:
                    response = _substitute_numbered_groups(
                        trigger.response_template, match.groups()
                    )
                event = TriggerFireEvent(
                    trigger_id=trigger.id,
                    pattern=trigger.pattern,
                    line=line,
                    matched_text=match.group(0),
                    response=response,
                    fired_at=time.time(),
                )
                for hook in tuple(self._trigger_fire_hooks):
                    try:
                        hook(event)
                    except Exception:
                        logger.exception("trigger fire hook raised")
                if trigger.gag:
                    display_line = None

        for hook in tuple(self._event_hooks):
            try:
                hook(line)
            except Exception:
                logger.exception("event hook raised")

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

        if not self.enabled:
            return [command]

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

        if not self.enabled:
            return

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

        self._attached_bus_subscriptions[bus_identity] = [
            bus.on(
                EventType.TEXT,
                lambda event: self.on_text(event.data),
            ),
            bus.on(
                EventType.PROMPT,
                lambda event: self.on_text(event.data),
            ),
        ]

    def detach(self, bus: "EventBus") -> None:
        """Release subscriptions installed by :meth:`attach`."""
        bus_identity = id(bus)
        subscriptions = self._attached_bus_subscriptions.pop(bus_identity, [])
        for subscription in subscriptions:
            subscription.close()
        self._attached_bus_ids.discard(bus_identity)
