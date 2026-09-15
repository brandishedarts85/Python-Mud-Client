from pathlib import Path


def test_initial_session_is_deferred_until_qasync_loop_turn():
    root = Path(__file__).resolve().parents[2]
    app_source = (root / "qt" / "app.py").read_text(encoding="utf-8")
    window_source = (root / "qt" / "main_window.py").read_text(encoding="utf-8")

    assert "window = MainWindow()" in app_source
    assert "loop.call_soon(partial(window.create_session, **session_settings))" in app_source
    assert "self.create_session(host, port)" not in window_source
