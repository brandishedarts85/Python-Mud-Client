from __future__ import annotations

import os
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PySide6 = pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

from client_settings import ClientSettings
from qt.dialogs.settings import SettingsDialog
from qt.output_view import MudOutputView


def _app():
    return QApplication.instance() or QApplication([])


def test_settings_dialog_round_trips_model_values():
    _app()
    settings = ClientSettings(
        font_family="DejaVu Sans Mono",
        font_size=12,
        output_foreground="#abcdef",
        output_background="#121212",
        scrollback_blocks=23456,
        timestamps=True,
        local_echo=False,
        default_auto_reconnect=False,
        default_reconnect_base_delay=4.0,
        default_reconnect_max_delay=40.0,
    )
    dialog = SettingsDialog(settings)
    try:
        round_trip = dialog.client_settings()
        assert round_trip.font_family
        assert round_trip.font_size == 12
        assert round_trip.output_foreground == "#abcdef"
        assert round_trip.output_background == "#121212"
        assert round_trip.scrollback_blocks == 23456
        assert round_trip.timestamps is True
        assert round_trip.local_echo is False
        assert round_trip.default_auto_reconnect is False
        assert round_trip.default_reconnect_base_delay == 4.0
        assert round_trip.default_reconnect_max_delay == 40.0
    finally:
        dialog.close()


def test_output_view_applies_appearance_and_bound():
    _app()
    view = MudOutputView()
    try:
        view.apply_appearance(
            font_family="DejaVu Sans Mono",
            font_size=14,
            foreground="#aabbcc",
            background="#101010",
            scrollback_blocks=4321,
            timestamps=True,
        )
        assert view.font().pointSize() == 14
        assert view.document().maximumBlockCount() == 4321
        assert view._timestamps_enabled is True
    finally:
        view.close()
