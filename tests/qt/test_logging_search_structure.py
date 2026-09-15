from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_main_window_exposes_logging_search_and_export_actions() -> None:
    source = (ROOT / "qt" / "main_window.py").read_text(encoding="utf-8")
    assert 'QAction("Find in Transcript…"' in source
    assert 'QAction("Start Session Logging…"' in source
    assert 'QAction("Stop Session Logging"' in source
    assert 'QAction("Export Transcript…"' in source
    assert 'setShortcut("Ctrl+F")' in source


def test_session_logging_failure_is_fail_soft() -> None:
    source = (ROOT / "qt" / "session_widget.py").read_text(encoding="utf-8")
    assert "except OSError as exc:" in source
    assert "self.stop_logging()" in source
    assert "self.logging_error.emit(str(exc))" in source
    assert "self.output.append_styled_line(line)" in source


def test_transcript_module_remains_qt_independent() -> None:
    source = (ROOT / "transcript.py").read_text(encoding="utf-8")
    assert "PySide6" not in source
    assert "qt." not in source
