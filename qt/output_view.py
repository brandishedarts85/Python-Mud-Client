"""ANSI-capable central MUD output widget."""

from __future__ import annotations

import json
from datetime import datetime

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor, QFont, QPalette, QTextCharFormat, QTextCursor, QTextDocument, QTextFormat
from PySide6.QtWidgets import QTextEdit

from ansi_parser import Style, StyledLine


_STYLE_METADATA_PROPERTY = int(QTextFormat.Property.UserProperty) + 37


def _style_payload(style: Style) -> str:
    return json.dumps(
        {
            "fg": list(style.fg) if style.fg is not None else None,
            "bg": list(style.bg) if style.bg is not None else None,
            "bold": style.bold,
            "dim": style.dim,
            "italic": style.italic,
            "underline": style.underline,
            "blink": style.blink,
            "strike": style.strike,
        },
        separators=(",", ":"),
    )


def _style_from_payload(value) -> Style | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        data = json.loads(value)
        if not isinstance(data, dict):
            return None
        fg = data.get("fg")
        bg = data.get("bg")
        return Style(
            fg=tuple(fg) if isinstance(fg, list) and len(fg) == 3 else None,
            bg=tuple(bg) if isinstance(bg, list) and len(bg) == 3 else None,
            bold=bool(data.get("bold", False)),
            dim=bool(data.get("dim", False)),
            italic=bool(data.get("italic", False)),
            underline=bool(data.get("underline", False)),
            blink=bool(data.get("blink", False)),
            strike=bool(data.get("strike", False)),
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


class MudOutputView(QTextEdit):
    """Read-only transcript with a capture-to-trigger context action."""

    trigger_capture_requested = Signal(str, object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setAcceptRichText(False)
        self.setUndoRedoEnabled(False)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.setFont(QFont("Consolas", 10))
        self.document().setMaximumBlockCount(10_000)
        self._timestamps_enabled = False
        self._has_output = False

    def append_styled_line(self, line: StyledLine) -> None:
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        if self._has_output:
            cursor.insertBlock()

        if self._timestamps_enabled:
            timestamp_style = Style(dim=True)
            cursor.insertText(
                f"[{datetime.now().strftime('%H:%M:%S')}] ",
                self._format_for(timestamp_style),
            )

        for segment in line.segments:
            cursor.insertText(segment.text, self._format_for(segment.style))

        self._has_output = True
        self.setTextCursor(cursor)
        self.ensureCursorVisible()

    def find_text(
        self,
        query: str,
        *,
        backward: bool = False,
        case_sensitive: bool = False,
    ) -> bool:
        """Select the next matching transcript occurrence, wrapping once."""
        if not query:
            return False

        flags = QTextDocument.FindFlag(0)
        if backward:
            flags |= QTextDocument.FindFlag.FindBackward
        if case_sensitive:
            flags |= QTextDocument.FindFlag.FindCaseSensitively

        cursor = self.textCursor()
        found = self.document().find(query, cursor, flags)
        if found.isNull():
            anchor = QTextCursor(self.document())
            if backward:
                anchor.movePosition(QTextCursor.MoveOperation.End)
            else:
                anchor.movePosition(QTextCursor.MoveOperation.Start)
            found = self.document().find(query, anchor, flags)
        if found.isNull():
            return False

        self.setTextCursor(found)
        self.ensureCursorVisible()
        return True

    def apply_appearance(
        self,
        *,
        font_family: str,
        font_size: int,
        foreground: str,
        background: str,
        scrollback_blocks: int,
        timestamps: bool,
    ) -> None:
        """Apply presentation preferences without touching parser semantics."""
        self.setFont(QFont(font_family, font_size))
        self.document().setMaximumBlockCount(scrollback_blocks)
        self._timestamps_enabled = bool(timestamps)

        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Text, QColor(foreground))
        palette.setColor(QPalette.ColorRole.Base, QColor(background))
        self.setPalette(palette)

    def contextMenuEvent(self, event) -> None:  # noqa: N802 - Qt API
        menu = self.createStandardContextMenu()
        cursor = self.textCursor()
        selected = cursor.selectedText().replace("\u2029", "\n")

        menu.addSeparator()
        capture = menu.addAction("Create Trigger from Selection…")
        capture.setEnabled(bool(selected) and "\n" not in selected)

        chosen = menu.exec(event.globalPos())
        if chosen is capture and selected and "\n" not in selected:
            styles = self._styles_in_selection(cursor)
            self.trigger_capture_requested.emit(selected, styles)

    def _styles_in_selection(self, selection: QTextCursor) -> tuple[Style, ...]:
        """Return the unique presentation styles used by visible characters.

        A selected MUD line may legitimately contain multiple colors (for
        example a white prompt with one yellow account name).  Capturing only
        the first character made the generated trigger impossible to satisfy
        because the matcher correctly requires every matched visible character
        to use an allowed style.
        """
        text = self.toPlainText()
        start = selection.selectionStart()
        end = selection.selectionEnd()
        styles: list[Style] = []

        for position in range(start, min(end, len(text))):
            if text[position].isspace():
                continue

            probe = QTextCursor(self.document())
            probe.setPosition(position)
            probe.movePosition(
                QTextCursor.MoveOperation.NextCharacter,
                QTextCursor.MoveMode.KeepAnchor,
            )
            style = self._style_from_format(probe.charFormat())
            if style not in styles:
                styles.append(style)

        return tuple(styles)

    @staticmethod
    def _style_from_format(fmt: QTextCharFormat) -> Style:
        # Prefer the original parser style preserved as QTextCharFormat metadata.
        # Reading painted QColor values is incorrect for trigger capture because
        # Qt resolves an unset ANSI foreground/background through the widget
        # palette (for example None -> visible #E5E5E5 / #000000).
        captured = _style_from_payload(fmt.property(_STYLE_METADATA_PROPERTY))
        if captured is not None:
            return captured

        # Compatibility fallback for documents produced by an older build.
        def color_from_brush(brush) -> tuple[int, int, int] | None:
            color = brush.color()
            if not color.isValid():
                return None
            return (color.red(), color.green(), color.blue())

        weight = fmt.fontWeight()
        return Style(
            fg=color_from_brush(fmt.foreground()),
            bg=color_from_brush(fmt.background()),
            bold=weight >= int(QFont.Weight.Bold),
            dim=weight <= int(QFont.Weight.Light),
            italic=fmt.fontItalic(),
            underline=fmt.fontUnderline(),
            strike=fmt.fontStrikeOut(),
        )

    @staticmethod
    def _format_for(style: Style) -> QTextCharFormat:
        fmt = QTextCharFormat()

        fg = style.fg
        bg = style.bg
        if style.reverse:
            fg, bg = bg, fg

        # Preserve the actual parsed/presentation style for capture.  We store
        # semantic colors after reverse-video resolution so the captured values
        # are exactly the colors the trigger matcher compares.
        presentation_style = Style(
            fg=fg,
            bg=bg,
            bold=style.bold,
            dim=style.dim,
            italic=style.italic,
            underline=style.underline,
            blink=style.blink,
            reverse=False,
            strike=style.strike,
        )
        fmt.setProperty(_STYLE_METADATA_PROPERTY, _style_payload(presentation_style))

        if fg is not None:
            fmt.setForeground(QColor(*fg))
        if bg is not None:
            fmt.setBackground(QColor(*bg))

        if style.bold:
            fmt.setFontWeight(QFont.Weight.Bold)
        elif style.dim:
            fmt.setFontWeight(QFont.Weight.Light)

        fmt.setFontItalic(style.italic)
        fmt.setFontUnderline(style.underline)
        fmt.setFontStrikeOut(style.strike)
        return fmt
