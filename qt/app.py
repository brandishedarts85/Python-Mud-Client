"""PySide6/qasync application entry point."""

from __future__ import annotations

import asyncio
import signal
import sys
from functools import partial

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox
from qasync import QEventLoop

from persistence import load_profiles_safely, resolve_connection
from qt.main_window import MainWindow


def _resolve_args(argv: list[str]) -> dict:
    arg1 = argv[1] if len(argv) > 1 else None
    arg2 = argv[2] if len(argv) > 2 else None
    if arg1 and not arg2:
        profiles, profile_error = load_profiles_safely()
        if profile_error is not None:
            return {
                "host": None,
                "port": None,
                "_startup_warning": (
                    "Connection profiles could not be loaded. "
                    "The file was left untouched.\n\n"
                    f"{profile_error}\n\n"
                    f"Saved profile {arg1!r} was not opened."
                ),
            }
        if arg1 in profiles:
            result = dict(profiles[arg1])
            result["profile_name"] = arg1
            return result
    host, port = resolve_connection(arg1, arg2)
    if host and port is None:
        raise SystemExit(
            f"'{host}' isn't a saved profile and no port was given.\n"
            "Usage: python app.py <host> <port>\n"
            "   or: python app.py <saved-profile-name>"
        )
    return {"host": host, "port": port}


def _cancel_remaining_tasks(loop: asyncio.AbstractEventLoop) -> None:
    """Cancel any straggling asyncio tasks before the qasync loop closes."""
    pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
    if not pending:
        return
    for task in pending:
        task.cancel()
    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))


def main() -> None:
    session_settings = _resolve_args(sys.argv)
    startup_warning = session_settings.pop("_startup_warning", None)

    app = QApplication(sys.argv)
    app.setApplicationName("Python MUD Client")
    app.setOrganizationName("brandished_arts")
    app.setQuitOnLastWindowClosed(True)

    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    # Construct widgets before entering the qasync loop, but defer creation
    # of the first session until the loop is actually running.
    window = MainWindow()
    window.show()
    loop.call_soon(partial(window.create_session, **session_settings))
    if startup_warning:
        QTimer.singleShot(
            0,
            lambda: QMessageBox.warning(
                window,
                "Connection Profiles",
                str(startup_warning),
            ),
        )

    # Explicitly stop qasync when the final application window closes.  Using
    # both Qt lifecycle signals makes teardown robust even with floating docks.
    app.lastWindowClosed.connect(loop.stop)
    app.aboutToQuit.connect(loop.stop)

    # Qt can otherwise spend long stretches inside native event processing on
    # Windows.  A lightweight timer returns control to Python regularly so
    # SIGINT (Ctrl+C in the launching console) is handled promptly.
    signal_timer = QTimer()
    signal_timer.setInterval(200)
    signal_timer.timeout.connect(lambda: None)
    signal_timer.start()

    previous_sigint = signal.getsignal(signal.SIGINT)

    def _handle_sigint(_signum, _frame) -> None:
        # Use the normal window shutdown path so transports/tasks are cleaned
        # up just as they are when the user closes the application normally.
        loop.call_soon_threadsafe(window.close)

    signal.signal(signal.SIGINT, _handle_sigint)

    try:
        with loop:
            try:
                loop.run_forever()
            finally:
                _cancel_remaining_tasks(loop)
    finally:
        signal_timer.stop()
        signal.signal(signal.SIGINT, previous_sigint)
        asyncio.set_event_loop(None)


if __name__ == "__main__":
    main()
