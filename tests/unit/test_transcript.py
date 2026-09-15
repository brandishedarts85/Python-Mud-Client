from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from transcript import TranscriptLogger, atomic_export_text


def test_logger_is_explicitly_started(tmp_path: Path) -> None:
    path = tmp_path / "session.log"
    logger = TranscriptLogger(path)
    logger.write_line("ignored before start")
    assert not path.exists()

    logger.start()
    assert logger.active
    logger.close()
    assert not logger.active
    assert path.exists()


def test_logger_writes_timestamped_utf8_lines(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "session.log"
    when = datetime(2026, 9, 14, 12, 30, 45, tzinfo=timezone.utc)
    logger = TranscriptLogger(path)
    logger.start()
    logger.write_line("Snowman ☃", when=when)
    logger.close()

    assert path.read_text(encoding="utf-8") == "[2026-09-14T12:30:45+00:00] Snowman ☃\n"


def test_logger_rotates_and_bounds_backups(tmp_path: Path) -> None:
    path = tmp_path / "session.log"
    logger = TranscriptLogger(path, max_bytes=75, backups=2)
    logger.start()
    when = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    for index in range(12):
        logger.write_line(f"line-{index:02d}-payload", when=when)
    logger.close()

    assert path.exists()
    assert Path(f"{path}.1").exists()
    assert Path(f"{path}.2").exists()
    assert not Path(f"{path}.3").exists()
    combined = "".join(
        candidate.read_text(encoding="utf-8")
        for candidate in (Path(f"{path}.2"), Path(f"{path}.1"), path)
    )
    assert "line-11-payload" in combined


def test_logger_rotation_with_zero_backups_discards_previous_segment(tmp_path: Path) -> None:
    path = tmp_path / "session.log"
    logger = TranscriptLogger(path, max_bytes=65, backups=0)
    logger.start()
    when = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    for index in range(4):
        logger.write_line(f"line-{index}-payload", when=when)
    logger.close()
    assert path.exists()
    assert not Path(f"{path}.1").exists()
    assert "line-3-payload" in path.read_text(encoding="utf-8")


def test_atomic_export_text_creates_parent_and_normalizes_final_newline(tmp_path: Path) -> None:
    path = tmp_path / "exports" / "transcript.txt"
    atomic_export_text(path, "alpha\nbeta")
    assert path.read_text(encoding="utf-8") == "alpha\nbeta\n"

    atomic_export_text(path, "replacement\n")
    assert path.read_text(encoding="utf-8") == "replacement\n"
