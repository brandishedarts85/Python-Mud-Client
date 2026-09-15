from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    QApplication = None

if QApplication is not None:
    import pytest

    @pytest.fixture(scope="session")
    def qapp():
        app = QApplication.instance() or QApplication([])
        return app
