"""UI-neutral outbound command boundary.

All command producers use a typed source.  Source policy is fixed here rather
than left to each UI/automation caller, keeping alias/history and Telnet ECHO
safety consistent as mapper/script producers are added later.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable, Iterable


class CommandSource(Enum):
    MANUAL = auto()
    MACRO = auto()
    AUTOMATION = auto()
    MAPPER = auto()
    SCRIPT = auto()


@dataclass(frozen=True)
class CommandRequest:
    text: str
    source: CommandSource = CommandSource.MANUAL


@dataclass(frozen=True)
class CommandPolicy:
    allow_client_commands: bool
    expand_aliases: bool
    record_history: bool
    local_echo: bool
    notify_if_disconnected: bool


@dataclass(frozen=True)
class CommandResult:
    request: CommandRequest
    sent: tuple[str, ...] = ()
    handled_client_command: bool = False
    rejected_disconnected: bool = False


_POLICIES = {
    # Macros historically used submit_command(), so they intentionally preserve
    # manual alias/history/client-command behavior.
    CommandSource.MANUAL: CommandPolicy(True, True, True, True, True),
    CommandSource.MACRO: CommandPolicy(True, True, True, True, True),
    # Automation-generated commands historically bypassed aliases/history and
    # could not invoke # client commands. Mapper/script defaults follow that
    # safer non-recursive behavior until explicit APIs are designed for them.
    CommandSource.AUTOMATION: CommandPolicy(False, False, False, True, False),
    CommandSource.MAPPER: CommandPolicy(False, False, False, True, False),
    CommandSource.SCRIPT: CommandPolicy(False, False, False, True, False),
}


class CommandPipeline:
    """Apply source policy, echo safety, expansion, and transport dispatch."""

    def __init__(
        self,
        *,
        is_connected: Callable[[], bool],
        server_echo_enabled: Callable[[], bool],
        send_line: Callable[[str], None],
        expand_aliases: Callable[[str], Iterable[str]],
        expand_variables: Callable[[str], str] | None = None,
        local_echo: Callable[[str], None],
        record_history: Callable[[str], None],
        handle_client_command: Callable[[str], None],
        notify_not_connected: Callable[[], None],
    ) -> None:
        self._is_connected = is_connected
        self._server_echo_enabled = server_echo_enabled
        self._send_line = send_line
        self._expand_aliases = expand_aliases
        self._expand_variables = expand_variables or (lambda text: text)
        self._local_echo = local_echo
        self._record_history = record_history
        self._handle_client_command = handle_client_command
        self._notify_not_connected = notify_not_connected

    def dispatch(self, request: CommandRequest) -> CommandResult:
        policy = _POLICIES[request.source]
        text = request.text

        if policy.allow_client_commands and text.startswith("#"):
            self._handle_client_command(text[1:].strip())
            return CommandResult(request, handled_client_command=True)

        if not self._is_connected():
            if policy.notify_if_disconnected:
                self._notify_not_connected()
            return CommandResult(request, rejected_disconnected=True)

        if text and policy.local_echo and not self._server_echo_enabled():
            self._local_echo(text)

        if text and policy.record_history:
            self._record_history(text)

        if text and policy.expand_aliases:
            outgoing = tuple(self._expand_aliases(text))
        else:
            # Empty lines remain meaningful to Telnet MUDs and must be sent.
            outgoing = (text,)

        outgoing = tuple(self._expand_variables(line) for line in outgoing)

        for line in outgoing:
            self._send_line(line)

        return CommandResult(request, sent=outgoing)
