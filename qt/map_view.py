from __future__ import annotations

from collections.abc import Callable, Iterable
import math

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QBrush, QPainter, QPalette, QPen, QPolygonF
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
)

from mapper import Exit, Room, RouteStep
from mapper_layout import compute_room_positions

ROOM_WIDTH = 118.0
ROOM_HEIGHT = 46.0


class MapRoomItem(QGraphicsRectItem):
    def __init__(
        self,
        room_id: int,
        on_selected: Callable[[int], None],
        on_moved: Callable[[int, float, float], None] | None = None,
    ):
        super().__init__(-ROOM_WIDTH / 2, -ROOM_HEIGHT / 2, ROOM_WIDTH, ROOM_HEIGHT)
        self.room_id = int(room_id)
        self._on_selected = on_selected
        self._on_moved = on_moved
        self._press_pos = None
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, on_moved is not None)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event):
        self._press_pos = self.pos()
        super().mousePressEvent(event)
        self._on_selected(self.room_id)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if self._on_moved is not None and self._press_pos is not None:
            pos = self.pos()
            if (pos - self._press_pos).manhattanLength() > 0.01:
                self._on_moved(self.room_id, pos.x(), pos.y())
        self._press_pos = None


class MapperGraphicsView(QGraphicsView):
    """Read-only graphical projection of mapper repository state.

    The widget deliberately has no controller or transport reference. Its only
    outbound action is a room-selection callback supplied by the parent dock.
    """

    def __init__(
        self,
        on_room_selected: Callable[[int], None],
        parent=None,
        *,
        on_room_moved: Callable[[int, float, float], None] | None = None,
    ):
        scene = QGraphicsScene(parent)
        super().__init__(parent)
        self.setScene(scene)
        self._on_room_selected = on_room_selected
        self._on_room_moved = on_room_moved
        self._selected_room_id: int | None = None
        self._room_items: dict[int, MapRoomItem] = {}
        self._has_fit_once = False
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setMinimumHeight(220)

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else (1.0 / 1.15)
        current = self.transform().m11()
        target = current * factor
        if 0.18 <= target <= 5.0:
            self.scale(factor, factor)
        event.accept()

    def fit_map(self) -> None:
        bounds = self.scene().itemsBoundingRect()
        if not bounds.isNull():
            self.fitInView(bounds.adjusted(-40, -40, 40, 40), Qt.AspectRatioMode.KeepAspectRatio)
            self._has_fit_once = True

    def select_room(self, room_id: int | None, *, center: bool = False) -> None:
        self._selected_room_id = room_id
        for rid, item in self._room_items.items():
            item.setSelected(room_id is not None and rid == room_id)
        if center and room_id in self._room_items:
            self.centerOn(self._room_items[room_id])

    def render_map(
        self,
        rooms: Iterable[Room],
        exits: Iterable[Exit],
        *,
        current_room_id: int | None,
        route_steps: Iterable[RouteStep] = (),
        selected_room_id: int | None = None,
    ) -> None:
        rooms = tuple(rooms)
        exits = tuple(exits)
        route_steps = tuple(route_steps)
        route_exit_ids = {step.exit_id for step in route_steps}
        positions = compute_room_positions(rooms)

        scene = self.scene()
        scene.clear()
        self._room_items = {}

        palette = QApplication.palette()
        normal_line = palette.color(QPalette.ColorRole.Mid)
        route_color = palette.color(QPalette.ColorRole.Highlight)
        node_base = palette.color(QPalette.ColorRole.Base)
        node_text = palette.color(QPalette.ColorRole.Text)
        current_base = palette.color(QPalette.ColorRole.Highlight)
        current_text = palette.color(QPalette.ColorRole.HighlightedText)

        # Edges first, so room nodes remain on top and selectable.
        for edge in exits:
            source = positions.get(edge.source_room_id)
            dest = positions.get(edge.destination_room_id)
            if source is None or dest is None:
                continue
            pen = QPen(route_color if edge.id in route_exit_ids else normal_line)
            pen.setWidthF(3.0 if edge.id in route_exit_ids else 1.4)
            if edge.kind == "door":
                pen.setStyle(Qt.PenStyle.DashLine)
            elif edge.kind == "special":
                pen.setStyle(Qt.PenStyle.DotLine)
            line_item = scene.addLine(source.x, source.y, dest.x, dest.y, pen)
            line_item.setToolTip(
                f"{edge.direction}: {edge.command} [{edge.kind}] cost={edge.cost:g}"
                + (f"\nPre-command: {edge.pre_command}" if edge.pre_command else "")
            )
            dx = dest.x - source.x
            dy = dest.y - source.y
            if dx or dy:
                angle = math.atan2(dy, dx)
                tip = QPointF(source.x + dx * 0.72, source.y + dy * 0.72)
                size = 10.0
                left = QPointF(
                    tip.x() - size * math.cos(angle - 0.55),
                    tip.y() - size * math.sin(angle - 0.55),
                )
                right = QPointF(
                    tip.x() - size * math.cos(angle + 0.55),
                    tip.y() - size * math.sin(angle + 0.55),
                )
                arrow = scene.addPolygon(QPolygonF((tip, left, right)), pen, QBrush(pen.color()))
                arrow.setToolTip(line_item.toolTip())

        for room in rooms:
            pos = positions[room.id]
            item = MapRoomItem(room.id, self._on_room_selected, self._on_room_moved)
            item.setPos(QPointF(pos.x, pos.y))
            is_current = room.id == current_room_id
            pen = QPen(route_color if is_current else normal_line)
            pen.setWidthF(3.2 if is_current else 1.3)
            item.setPen(pen)
            item.setBrush(QBrush(current_base if is_current else node_base))
            item.setToolTip(
                f"#{room.id} {room.name}"
                + (f"\nArea: {room.area}" if room.area else "")
                + (f"\nExternal ID: {room.external_id}" if room.external_id else "")
                + (f"\nCoordinates: {room.x}, {room.y}, {room.z}" if room.x is not None else "\nCoordinates: automatic layout")
                + (f"\nManual layout: {room.layout_x:g}, {room.layout_y:g}" if room.layout_x is not None and room.layout_y is not None else "")
            )
            scene.addItem(item)
            self._room_items[room.id] = item

            display_name = room.name if len(room.name) <= 24 else room.name[:21] + "…"
            label = QGraphicsSimpleTextItem(display_name, item)
            label.setBrush(QBrush(current_text if is_current else node_text))
            label_bounds = label.boundingRect()
            label.setPos(-min(label_bounds.width(), ROOM_WIDTH - 8) / 2, -label_bounds.height() / 2)

        scene.setSceneRect(scene.itemsBoundingRect().adjusted(-80, -80, 80, 80))
        self.select_room(selected_room_id)
        if rooms and not self._has_fit_once:
            self.fit_map()
