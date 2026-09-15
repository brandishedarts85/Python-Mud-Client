"""Transcript logging and export helpers.

This module is intentionally UI-neutral.  Qt chooses paths and presents errors;
these helpers own durable UTF-8 transcript I/O and bounded log rotation.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import TextIO


DEFAULT_LOG_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_LOG_BACKUPS = 3


class TranscriptLogger:
    """Append-only visible-session logger with bounded size rotation.

    Rotation uses ``path.1``, ``path.2`` ... where ``.1`` is the most recent
    previous segment.  A line is never intentionally split across segments.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_bytes: int = DEFAULT_LOG_MAX_BYTES,
        backups: int = DEFAULT_LOG_BACKUPS,
    ) -> None:
        self.path = Path(path)
        if max_bytes <= 0:
            raise ValueError("max_bytes must be greater than zero")
        if backups < 0:
            raise ValueError("backups must not be negative")
        self.max_bytes = int(max_bytes)
        self.backups = int(backups)
        self._file: TextIO | None = None

    @property
    def active(self) -> bool:
        return self._file is not None

    def start(self) -> None:
        if self._file is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a", encoding="utf-8", newline="\n")

    def close(self) -> None:
        file, self._file = self._file, None
        if file is None:
            return
        try:
            file.flush()
            os.fsync(file.fileno())
        finally:
            file.close()

    def write_line(self, text: str, *, when: datetime | None = None) -> None:
        if self._file is None:
            return
        value = when or datetime.now()
        if value.tzinfo is not None:
            stamp = value.isoformat(timespec="seconds")
        else:
            stamp = value.astimezone().isoformat(timespec="seconds")
        payload = f"[{stamp}] {text}\n"
        encoded_size = len(payload.encode("utf-8"))
        self._rotate_if_needed(encoded_size)
        assert self._file is not None
        self._file.write(payload)
        self._file.flush()

    def _rotate_if_needed(self, incoming_bytes: int) -> None:
        file = self._file
        if file is None:
            return
        try:
            current_size = self.path.stat().st_size
        except FileNotFoundError:
            current_size = 0
        if current_size == 0 or current_size + incoming_bytes <= self.max_bytes:
            return

        file.flush()
        os.fsync(file.fileno())
        file.close()
        self._file = None

        if self.backups == 0:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        else:
            oldest = Path(f"{self.path}.{self.backups}")
            try:
                oldest.unlink()
            except FileNotFoundError:
                pass
            for index in range(self.backups - 1, 0, -1):
                source = Path(f"{self.path}.{index}")
                target = Path(f"{self.path}.{index + 1}")
                if source.exists():
                    os.replace(source, target)
            if self.path.exists():
                os.replace(self.path, Path(f"{self.path}.1"))

        self._file = self.path.open("a", encoding="utf-8", newline="\n")

    def __enter__(self) -> "TranscriptLogger":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def atomic_export_text(path: str | Path, text: str) -> None:
    """Atomically export UTF-8 text to a user-selected destination."""

    target = Path(path)
    parent = target.parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)
    temp_dir = parent if str(parent) not in ("", ".") else Path(".")

    fd: int | None = None
    temp_name: str | None = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=temp_dir, text=True
        )
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            fd = None
            file.write(text)
            if text and not text.endswith("\n"):
                file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, target)
        temp_name = None
    finally:
        if fd is not None:
            os.close(fd)
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
