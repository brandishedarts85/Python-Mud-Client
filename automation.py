"""
automation.py -- the brain.

Triggers, aliases, timers, and a plugin hook API, all driven off the
events that client_core/ansi_parser produce. This is deliberately the
opposite of GMUD32's approach: instead of a bespoke mini-language,
patterns are plain Python regexes and actions are plain Python
callables (or, for simple cases, template strings) -- so the "trigger
syntax" is just "if this matches, run this," with none of a scripting
DSL's ceremony, and the full power of Python is one function away.

Contains:
  - Alias:   command word -> expansion (with %1..%9 / *args substitution)
  - Trigger: regex against incoming text -> action, with gag/one-shot/
             cooldown support
  - Timer:   one-shot or repeating callback
  - AutomationEngine: owns all of the above, subscribes to an EventBus,
    and exposes register_* / list_* / enable/disable methods a UI or
    plugin can call.

Nothing here talks to the network or the screen directly -- it only
calls back into whatever `send_line` callable it's given.
"""

from __future__ import annotations

import re
import time
import uuid
import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger("mudclient.automation")

SendFn = Callable[[str], None]

try:  # only needed if the automation layer is wired to client_core's bus
    from client_core import EventBus, EventType
except ImportError:  # pragma: no cover - allows standalone unit testing
    EventBus = None  # type: ignore
    EventType = None  # type: ignore


# --------------------------------------------------------------------------
# Aliases
# --------------------------------------------------------------------------


@dataclass
class Alias:
    id: str
    pattern: str                       # the typed command, e.g. "kk" or "gt *"
    expansion: str | Callable[[list[str]], str]
    enabled: bool = True
    _regex: re.Pattern = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # "*" in an alias pattern captures "everything after this point"
        # (classic MUD-client alias syntax); word-splitting otherwise.
        escaped = re.escape(self.pattern)
        escaped = escaped.replace(r"\*", "(.*)")
        self._regex = re.compile(rf"^{escaped}$", re.IGNORECASE)

    def matches(self, command: str) -> Optional[re.Match]:
        if not self.enabled:
            return None
        return self._regex.match(command)

    def expand(self, match: re.Match) -> str:
        groups = list(match.groups())
        if callable(self.expansion):
            return self.expansion(groups)
        text = self.expansion
        for i, g in enumerate(groups, start=1):
            text = text.replace(f"%{i}", g or "")
        return text


# --------------------------------------------------------------------------
# Triggers
# --------------------------------------------------------------------------


@dataclass
class Trigger:
    id: str
    pattern: str                                   # regex against a line of game text
    action: Callable[[re.Match, "TriggerContext"], None]
    enabled: bool = True
    gag: bool = False                               # suppress the matched line from display
    one_shot: bool = False                          # auto-disable after first fire
    cooldown_s: float = 0.0                          # minimum seconds between fires
    case_sensitive: bool = False
    _regex: re.Pattern = field(init=False, repr=False)
    _last_fired: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:
        flags = 0 if self.case_sensitive else re.IGNORECASE
        self._regex = re.compile(self.pattern, flags)

    def check(self, line: str) -> Optional[re.Match]:
        if not self.enabled:
            return None
        if self.cooldown_s and (time.monotonic() - self._last_fired) < self.cooldown_s:
            return None
        return self._regex.search(line)

    def fire(self, match: re.Match, ctx: "TriggerContext") -> None:
        self._last_fired = time.monotonic()
        try:
            self.action(match, ctx)
        except Exception:
            logger.exception("trigger %s action raised", self.id)
        if self.one_shot:
            self.enabled = False


@dataclass
class TriggerContext:
    """Passed to every trigger action so it can send commands, chain
    into other triggers/aliases, or read the raw line without needing
    a reference to the whole engine."""
    line: str
    send: SendFn
    engine: "AutomationEngine"


# --------------------------------------------------------------------------
# Timers
# --------------------------------------------------------------------------


@dataclass
class Timer:
    id: str
    interval_s: float
    action: Callable[[], None]
    repeat: bool = True
    enabled: bool = True
    _next_fire: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:
        self._next_fire = time.monotonic() + self.interval_s

    def due(self, now: float) -> bool:
        return self.enabled and now >= self._next_fire

    def fire(self) -> None:
        try:
            self.action()
        except Exception:
            logger.exception("timer %s action raised", self.id)
        if self.repeat:
            self._next_fire = time.monotonic() + self.interval_s
        else:
            self.enabled = False


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------


class AutomationEngine:
    """
    Owns triggers/aliases/timers and drives them off incoming text and
    a wall clock. Wire it up like:

        engine = AutomationEngine(send_fn=conn.send_line)
        bus.on(EventType.TEXT, lambda e: engine.on_text(e.data))
        bus.on(EventType.PROMPT, lambda e: engine.on_text(e.data))
        # in your event loop / ticker:
        engine.tick()

    Plugins register hooks the same way a UI would: through
    add_trigger / add_alias / add_timer / add_event_hook.
    """

    def __init__(self, send_fn: SendFn) -> None:
        self.send = send_fn
        self.triggers: dict[str, Trigger] = {}
        self.aliases: dict[str, Alias] = {}
        self.timers: dict[str, Timer] = {}
        # plugin hook: raw text -> None; called on every line, after triggers
        self._event_hooks: list[Callable[[str], None]] = []

    # -- registration ----------------------------------------------------

    def add_trigger(
        self,
        pattern: str,
        action: Callable[[re.Match, TriggerContext], None],
        *,
        gag: bool = False,
        one_shot: bool = False,
        cooldown_s: float = 0.0,
        case_sensitive: bool = False,
        trigger_id: Optional[str] = None,
    ) -> str:
        tid = trigger_id or str(uuid.uuid4())
        self.triggers[tid] = Trigger(
            id=tid,
            pattern=pattern,
            action=action,
            gag=gag,
            one_shot=one_shot,
            cooldown_s=cooldown_s,
            case_sensitive=case_sensitive,
        )
        return tid

    def add_alias(
        self,
        pattern: str,
        expansion: str | Callable[[list[str]], str],
        *,
        alias_id: Optional[str] = None,
    ) -> str:
        aid = alias_id or str(uuid.uuid4())
        self.aliases[aid] = Alias(id=aid, pattern=pattern, expansion=expansion)
        return aid

    def add_timer(
        self,
        interval_s: float,
        action: Callable[[], None],
        *,
        repeat: bool = True,
        timer_id: Optional[str] = None,
    ) -> str:
        tmid = timer_id or str(uuid.uuid4())
        self.timers[tmid] = Timer(id=tmid, interval_s=interval_s, action=action, repeat=repeat)
        return tmid

    def add_event_hook(self, hook: Callable[[str], None]) -> None:
        """For plugins that want every line without writing a regex."""
        self._event_hooks.append(hook)

    # -- enable/disable/remove --------------------------------------------

    def set_enabled(self, kind: str, id_: str, enabled: bool) -> None:
        store = {"trigger": self.triggers, "alias": self.aliases, "timer": self.timers}[kind]
        if id_ in store:
            store[id_].enabled = enabled

    def remove(self, kind: str, id_: str) -> None:
        store = {"trigger": self.triggers, "alias": self.aliases, "timer": self.timers}[kind]
        store.pop(id_, None)

    # -- driving from events ----------------------------------------------

    def on_text(self, line: str) -> Optional[str]:
        """Call this for every line of incoming game text (plain text,
        after ANSI stripping if you want gag to hide styling too).
        Returns the line to display, or None if it was gagged."""
        display_line: Optional[str] = line
        ctx = TriggerContext(line=line, send=self.send, engine=self)
        for trig in list(self.triggers.values()):
            match = trig.check(line)
            if match:
                trig.fire(match, ctx)
                if trig.gag:
                    display_line = None
        for hook in self._event_hooks:
            try:
                hook(line)
            except Exception:
                logger.exception("event hook raised")
        return display_line

    def on_command(self, command: str) -> list[str]:
        """Call this with whatever the user typed into the input bar,
        before sending it to the server. Returns the list of commands
        to actually send (an alias can expand to zero or more)."""
        for alias in self.aliases.values():
            match = alias.matches(command)
            if match:
                expanded = alias.expand(match)
                # allow an alias to expand to multiple semicolon-separated commands
                return [c.strip() for c in expanded.split(";;") if c.strip()]
        return [command]

    def tick(self) -> None:
        """Call periodically (e.g. every 100ms from your UI's event loop
        or an asyncio task) to fire due timers."""
        now = time.monotonic()
        for timer in list(self.timers.values()):
            if timer.due(now):
                timer.fire()

    # -- convenience wiring to client_core's EventBus ----------------------

    def attach(self, bus: "EventBus") -> None:
        """If client_core is available, subscribe directly to its bus so
        the caller doesn't have to wire on_text by hand."""
        if EventBus is None:
            raise RuntimeError("client_core.EventBus not importable")
        bus.on(EventType.TEXT, lambda e: self.on_text(e.data))
        bus.on(EventType.PROMPT, lambda e: self.on_text(e.data))
