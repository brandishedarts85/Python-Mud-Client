from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from ansi_parser import Segment, Style, StyledLine
from qt.output_view import MudOutputView


def test_output_find_wraps_and_supports_case_sensitive(qapp) -> None:
    view = MudOutputView()
    view.append_styled_line(StyledLine([Segment("Alpha", Style())]))
    view.append_styled_line(StyledLine([Segment("beta", Style())]))
    view.append_styled_line(StyledLine([Segment("ALPHA", Style())]))

    assert view.find_text("alpha")
    assert view.textCursor().selectedText() == "Alpha"
    assert view.find_text("alpha")
    assert view.textCursor().selectedText() == "ALPHA"
    assert view.find_text("alpha")
    assert view.textCursor().selectedText() == "Alpha"

    assert not view.find_text("alpha", case_sensitive=True)
