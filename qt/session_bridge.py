"""Thin QObject bridge over the UI-neutral MudSessionController."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from lifecycle import Subscription
from session_controller import MudSessionController


class QtSessionBridge(QObject):
    line_received = Signal(object)
    system_line = Signal(object)
    state_changed = Signal(object)
    gmcp_changed = Signal(object)
    msdp_changed = Signal(object)
    telnet_changed = Signal(object)
    automation_changed = Signal()

    def __init__(self, controller: MudSessionController, parent=None) -> None:
        super().__init__(parent)
        self.controller = controller
        self._subscriptions: list[Subscription] = [
            controller.on("line", self.line_received.emit),
            controller.on("system", self.system_line.emit),
            controller.on("state", self.state_changed.emit),
            controller.on("gmcp", self.gmcp_changed.emit),
            controller.on("msdp", self.msdp_changed.emit),
            controller.on("telnet", self.telnet_changed.emit),
            controller.on(
                "automation",
                lambda _payload: self.automation_changed.emit(),
            ),
        ]

        # Keep teardown correct even if this bridge is destroyed outside the
        # normal tab-close path.  The lambda captures only the handle list.
        subscriptions = self._subscriptions
        self.destroyed.connect(
            lambda *_args, subscriptions=subscriptions: QtSessionBridge._close_subscriptions(
                subscriptions
            )
        )

    @staticmethod
    def _close_subscriptions(subscriptions: list[Subscription]) -> None:
        for subscription in tuple(subscriptions):
            subscription.close()
        subscriptions.clear()

    def dispose(self) -> None:
        """Detach controller listeners before the QObject is destroyed."""
        self._close_subscriptions(self._subscriptions)
