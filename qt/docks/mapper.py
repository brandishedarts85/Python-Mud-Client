from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDockWidget,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from lifecycle import Subscription
from mapper import EXIT_KINDS, Exit, Room, RoutePreferences
from qt.map_view import MapperGraphicsView
from session_controller import MudSessionController


class MapperDock(QDockWidget):
    """Profile-scoped mapper editor and controlled route-walking surface."""

    def __init__(self, parent=None):
        super().__init__("Mapper", parent)
        self.setObjectName("MapperDock")
        self.controller: MudSessionController | None = None
        self._subscription: Subscription | None = None
        self._preview_route_steps = ()

        body = QWidget(self)
        root = QVBoxLayout(body)
        self.scope = QLabel("Scope: -", body)
        self.walk_status = QLabel("Walk: idle", body)
        root.addWidget(self.scope)
        root.addWidget(self.walk_status)

        adapter_row = QHBoxLayout()
        adapter_row.addWidget(QLabel("Room adapter:", body))
        self.adapter = QComboBox(body)
        adapter_row.addWidget(self.adapter, 1)
        root.addLayout(adapter_row)

        splitter = QSplitter(Qt.Orientation.Vertical, body)
        area_row = QHBoxLayout()
        area_row.addWidget(QLabel("View area:", body))
        self.area_filter = QComboBox(body)
        area_row.addWidget(self.area_filter, 1)
        self.route_preferences = QPushButton("Route Preferences...", body)
        self.clear_layout = QPushButton("Clear Room Layout", body)
        area_row.addWidget(self.route_preferences)
        area_row.addWidget(self.clear_layout)
        root.addLayout(area_row)

        self.map_view = MapperGraphicsView(
            self._map_room_selected,
            splitter,
            on_room_moved=self._map_room_moved,
        )
        splitter.addWidget(self.map_view)

        self.rooms = QTableWidget(0, 6, splitter)
        self.rooms.setHorizontalHeaderLabels(
            ("ID", "Current", "External ID", "Area", "Room", "Coords")
        )
        self.rooms.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.rooms.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        splitter.addWidget(self.rooms)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)

        row = QHBoxLayout()
        self.add_room = QPushButton("Add Room...", body)
        self.edit_room = QPushButton("Edit Room...", body)
        self.add_exit = QPushButton("Add Exit...", body)
        self.edit_exit = QPushButton("Edit Exit...", body)
        self.route = QPushButton("Route...", body)
        self.send_exit = QPushButton("Send Exit...", body)
        self.walk = QPushButton("Walk to Selected", body)
        self.stop_walk = QPushButton("Stop Walk", body)
        self.fit_map = QPushButton("Fit Map", body)
        self.center_current = QPushButton("Center Current", body)
        for button in (
            self.add_room,
            self.edit_room,
            self.add_exit,
            self.edit_exit,
            self.route,
            self.send_exit,
            self.walk,
            self.stop_walk,
            self.fit_map,
            self.center_current,
        ):
            row.addWidget(button)
        root.addLayout(row)
        self.setWidget(body)

        self.add_room.clicked.connect(self._add_room)
        self.edit_room.clicked.connect(self._edit_room)
        self.add_exit.clicked.connect(self._add_exit)
        self.edit_exit.clicked.connect(self._edit_exit)
        self.route.clicked.connect(self._route)
        self.send_exit.clicked.connect(self._send_exit)
        self.walk.clicked.connect(self._walk_selected)
        self.stop_walk.clicked.connect(self._stop_walking)
        self.fit_map.clicked.connect(self.map_view.fit_map)
        self.center_current.clicked.connect(self._center_current_room)
        self.rooms.itemSelectionChanged.connect(self._table_selection_changed)
        self.adapter.currentIndexChanged.connect(self._adapter_changed)
        self.area_filter.currentIndexChanged.connect(self.refresh)
        self.route_preferences.clicked.connect(self._edit_route_preferences)
        self.clear_layout.clicked.connect(self._clear_room_layout)
        self.refresh()

    def dispose(self):
        self._preview_route_steps = ()
        if self._subscription:
            self._subscription.close()
            self._subscription = None
        self.controller = None

    def set_controller(self, controller):
        if controller is self.controller:
            self.refresh()
            return
        if self._subscription:
            self._subscription.close()
            self._subscription = None
        self.controller = controller
        self._preview_route_steps = ()
        if controller:
            self._subscription = controller.on("mapper", lambda _payload: self.refresh())
        self.refresh()

    def _selected_room_id(self):
        row = self.rooms.currentRow()
        if row < 0:
            return None
        item = self.rooms.item(row, 0)
        return int(item.text()) if item else None

    def _map_room_selected(self, room_id: int) -> None:
        for row in range(self.rooms.rowCount()):
            item = self.rooms.item(row, 0)
            if item is not None and int(item.text()) == int(room_id):
                self.rooms.selectRow(row)
                self.rooms.scrollToItem(item)
                return

    def _map_room_moved(self, room_id: int, x: float, y: float) -> None:
        controller = self.controller
        if controller is None:
            return
        try:
            controller.mapper.set_room_layout(room_id, x, y)
            controller.mapper_changed()
        except Exception as exc:
            QMessageBox.warning(self, "Mapper", str(exc))

    def _clear_room_layout(self) -> None:
        controller = self.controller
        room_id = self._selected_room_id()
        if controller is None or room_id is None:
            QMessageBox.information(self, "Mapper", "Select a room first.")
            return
        try:
            controller.mapper.set_room_layout(room_id, None, None)
            controller.mapper_changed()
        except Exception as exc:
            QMessageBox.warning(self, "Mapper", str(exc))

    def _edit_route_preferences(self) -> None:
        controller = self.controller
        if controller is None:
            return
        prefs = controller.mapper.route_preferences
        avoid_areas, ok = QInputDialog.getText(
            self, "Route Preferences", "Avoid areas (comma-separated):",
            text=", ".join(prefs.avoid_areas),
        )
        if not ok:
            return
        avoid_tags, ok = QInputDialog.getText(
            self, "Route Preferences", "Avoid exit tags (comma-separated):",
            text=", ".join(prefs.avoid_tags),
        )
        if not ok:
            return
        prefer_tags, ok = QInputDialog.getText(
            self, "Route Preferences", "Prefer exit tags (comma-separated):",
            text=", ".join(prefs.prefer_tags),
        )
        if not ok:
            return
        multiplier, ok = QInputDialog.getDouble(
            self, "Route Preferences",
            "Preferred-tag cost multiplier (smaller = stronger preference):",
            prefs.prefer_multiplier, 0.01, 1.0, 2,
        )
        if not ok:
            return
        split = lambda value: tuple(part.strip() for part in value.split(",") if part.strip())
        try:
            controller.mapper.set_route_preferences(
                RoutePreferences(
                    avoid_areas=split(avoid_areas),
                    avoid_tags=split(avoid_tags),
                    prefer_tags=split(prefer_tags),
                    prefer_multiplier=multiplier,
                )
            )
            controller.mapper_changed()
        except Exception as exc:
            QMessageBox.warning(self, "Mapper", str(exc))

    def _table_selection_changed(self) -> None:
        self.map_view.select_room(self._selected_room_id())

    def _center_current_room(self) -> None:
        controller = self.controller
        if controller is None or controller.current_room_id is None:
            return
        self._map_room_selected(controller.current_room_id)
        self.map_view.select_room(controller.current_room_id, center=True)

    def refresh(self):
        controller = self.controller
        selected_room_id = self._selected_room_id()
        all_rooms = () if controller is None else controller.mapper.rooms()

        previous_area = self.area_filter.currentData()
        self.area_filter.blockSignals(True)
        self.area_filter.clear()
        self.area_filter.addItem("All areas", "")
        if controller is not None:
            for area in controller.mapper.areas():
                self.area_filter.addItem(area, area)
        area_index = self.area_filter.findData(previous_area)
        self.area_filter.setCurrentIndex(area_index if area_index >= 0 else 0)
        self.area_filter.setEnabled(controller is not None)
        self.area_filter.blockSignals(False)
        area = str(self.area_filter.currentData() or "")
        rooms = tuple(room for room in all_rooms if not area or room.area == area)
        self.rooms.setRowCount(len(rooms))

        self.adapter.blockSignals(True)
        self.adapter.clear()
        if controller is not None:
            for key, label in controller.mapper_adapter_choices:
                self.adapter.addItem(label, key)
            index = self.adapter.findData(controller.mapper_adapter_key)
            if index >= 0:
                self.adapter.setCurrentIndex(index)
        self.adapter.setEnabled(controller is not None)
        self.adapter.blockSignals(False)

        for row, room in enumerate(rooms):
            coords = "" if room.x is None else f"{room.x},{room.y},{room.z}"
            current = "*" if controller and room.id == controller.current_room_id else ""
            for column, value in enumerate(
                (room.id, current, room.external_id or "", room.area, room.name, coords)
            ):
                self.rooms.setItem(row, column, QTableWidgetItem(str(value)))
        self.rooms.resizeColumnsToContents()
        if selected_room_id is not None:
            for row in range(self.rooms.rowCount()):
                item = self.rooms.item(row, 0)
                if item is not None and int(item.text()) == selected_room_id:
                    self.rooms.selectRow(row)
                    break
        route_steps = ()
        if controller is not None:
            route_steps = controller.mapper_walker.route_steps or self._preview_route_steps
        visible_room_ids = {room.id for room in rooms}
        visible_exits = () if controller is None else tuple(
            edge for edge in controller.mapper.all_exits()
            if edge.source_room_id in visible_room_ids and edge.destination_room_id in visible_room_ids
        )
        self.map_view.render_map(
            rooms,
            visible_exits,
            current_room_id=None if controller is None else controller.current_room_id,
            route_steps=route_steps,
            selected_room_id=selected_room_id,
        )
        self.scope.setText(
            "Scope: -" if controller is None else f"Scope: {controller.mapper_scope_label}"
        )

        status = None if controller is None else controller.mapper_walk_status
        self.walk_status.setText(
            "Walk: idle" if status is None else f"Walk: {status.message}"
        )
        for button in (
            self.add_room,
            self.edit_room,
            self.add_exit,
            self.edit_exit,
            self.route,
            self.send_exit,
        ):
            button.setEnabled(controller is not None)
        self.walk.setEnabled(
            controller is not None and status is not None and not status.active
        )
        self.stop_walk.setEnabled(
            controller is not None and status is not None and status.active
        )
        self.route_preferences.setEnabled(controller is not None)
        self.clear_layout.setEnabled(controller is not None and selected_room_id is not None)

    def _adapter_changed(self):
        controller = self.controller
        if controller is None:
            return
        key = self.adapter.currentData()
        if key and key != controller.mapper_adapter_key:
            try:
                controller.set_mapper_adapter(str(key))
            except Exception as exc:
                QMessageBox.warning(self, "Mapper", str(exc))

    def _add_room(self):
        controller = self.controller
        if not controller:
            return
        name, ok = QInputDialog.getText(self, "Add Room", "Room name:")
        if not ok or not name.strip():
            return
        area, _ = QInputDialog.getText(self, "Add Room", "Area (optional):")
        try:
            controller.mapper.add_room(name, area=area)
            controller.mapper_changed()
        except Exception as exc:
            QMessageBox.warning(self, "Mapper", str(exc))

    def _edit_room(self):
        controller = self.controller
        room_id = self._selected_room_id()
        if not controller or room_id is None:
            QMessageBox.information(self, "Mapper", "Select a room first.")
            return
        try:
            room = controller.mapper.get_room(room_id)
        except KeyError:
            return
        name, ok = QInputDialog.getText(self, "Edit Room", "Room name:", text=room.name)
        if not ok or not name.strip():
            return
        area, ok = QInputDialog.getText(self, "Edit Room", "Area:", text=room.area)
        if not ok:
            return

        values = []
        for label, current in (("X coordinate (blank = automatic):", room.x),
                               ("Y coordinate (blank = automatic):", room.y),
                               ("Z/floor (blank = 0/automatic):", room.z)):
            text, ok = QInputDialog.getText(
                self, "Edit Room", label, text="" if current is None else str(current)
            )
            if not ok:
                return
            text = text.strip()
            try:
                values.append(None if not text else int(text))
            except ValueError:
                QMessageBox.warning(self, "Mapper", "Coordinates must be whole numbers or blank.")
                return
        try:
            controller.mapper.update_room(
                Room(
                    room.id,
                    name,
                    area,
                    values[0],
                    values[1],
                    values[2],
                    room.notes,
                    room.external_id,
                    room.source,
                    room.layout_x,
                    room.layout_y,
                )
            )
            controller.mapper_changed()
        except Exception as exc:
            QMessageBox.warning(self, "Mapper", str(exc))

    def _choose_room(self, title, label):
        controller = self.controller
        if not controller:
            return None
        rooms = controller.mapper.rooms()
        labels = [
            f'{room.id}: {room.area + " / " if room.area else ""}{room.name}'
            for room in rooms
        ]
        if not labels:
            return None
        choice, ok = QInputDialog.getItem(self, title, label, labels, 0, False)
        if not ok:
            return None
        return int(choice.split(":", 1)[0])

    def _choose_exit(self, room_id: int):
        controller = self.controller
        if controller is None:
            return None
        exits = controller.mapper.exits_from(room_id)
        if not exits:
            return None
        labels = [
            f"{edge.direction} -> {controller.mapper.get_room(edge.destination_room_id).name} "
            f"[{edge.kind}: {edge.command}]"
            + (f" tags={edge.tags}" if edge.tags else "")
            for edge in exits
        ]
        choice, ok = QInputDialog.getItem(self, "Choose Exit", "Exit:", labels, 0, False)
        if not ok:
            return None
        return exits[labels.index(choice)]

    def _exit_fields(self, *, title: str, edge: Exit | None = None):
        direction, ok = QInputDialog.getText(
            self,
            title,
            "Direction label:",
            text="" if edge is None else edge.direction,
        )
        if not ok or not direction.strip():
            return None
        command, ok = QInputDialog.getText(
            self,
            title,
            "Movement command:",
            text=direction if edge is None else edge.command,
        )
        if not ok or not command.strip():
            return None
        initial_kind = 0 if edge is None else EXIT_KINDS.index(edge.kind)
        kind, ok = QInputDialog.getItem(
            self, title, "Exit type:", list(EXIT_KINDS), initial_kind, False
        )
        if not ok:
            return None
        pre_command, ok = QInputDialog.getText(
            self,
            title,
            "Pre-command (optional; e.g. open north):",
            text="" if edge is None or edge.pre_command is None else edge.pre_command,
        )
        if not ok:
            return None
        tags, ok = QInputDialog.getText(
            self,
            title,
            "Route tags (space/comma separated; e.g. safe scenic risky):",
            text="" if edge is None else edge.tags,
        )
        if not ok:
            return None
        cost, ok = QInputDialog.getDouble(
            self,
            title,
            "Route cost:",
            1.0 if edge is None else edge.cost,
            0.001,
            1_000_000.0,
            3,
        )
        if not ok:
            return None
        return direction, command, str(kind), pre_command or None, tags, cost

    def _add_exit(self):
        controller = self.controller
        source = self._selected_room_id() or self._choose_room("Add Exit", "Source room:")
        if not controller or source is None:
            return
        destination = self._choose_room("Add Exit", "Destination room:")
        if destination is None:
            return
        fields = self._exit_fields(title="Add Exit")
        if fields is None:
            return
        direction, command, kind, pre_command, tags, cost = fields
        try:
            controller.mapper.add_exit(
                source,
                destination,
                direction,
                command=command,
                kind=kind,
                pre_command=pre_command,
                tags=tags,
                cost=cost,
            )
            controller.mapper_changed()
        except Exception as exc:
            QMessageBox.warning(self, "Mapper", str(exc))

    def _edit_exit(self):
        controller = self.controller
        source = self._selected_room_id()
        if not controller or source is None:
            QMessageBox.information(self, "Mapper", "Select a source room first.")
            return
        edge = self._choose_exit(source)
        if edge is None:
            QMessageBox.information(self, "Mapper", "That room has no exits.")
            return
        destination = self._choose_room("Edit Exit", "Destination room:")
        if destination is None:
            return
        fields = self._exit_fields(title="Edit Exit", edge=edge)
        if fields is None:
            return
        direction, command, kind, pre_command, tags, cost = fields
        try:
            controller.mapper.update_exit(
                Exit(
                    edge.id,
                    source,
                    destination,
                    direction,
                    command,
                    cost,
                    kind,
                    pre_command,
                    tags,
                )
            )
            controller.mapper_changed()
        except Exception as exc:
            QMessageBox.warning(self, "Mapper", str(exc))

    def _route(self):
        controller = self.controller
        source = self._selected_room_id() or self._choose_room("Route", "Start room:")
        if not controller or source is None:
            return
        destination = self._choose_room("Route", "Destination room:")
        if destination is None:
            return
        try:
            steps = controller.mapper.find_route(source, destination)
        except Exception as exc:
            QMessageBox.information(self, "Route", str(exc))
            return
        self._preview_route_steps = tuple(steps)
        self.refresh()
        if not steps:
            text = "Already there."
        else:
            lines = []
            for index, step in enumerate(steps, 1):
                prep = f"; prep: {step.pre_command}" if step.pre_command else ""
                tags = f" tags={step.tags}" if step.tags else ""
                lines.append(
                    f"{index}. {step.direction} [{step.kind}: {step.command}{prep}{tags}]"
                )
            text = "\n".join(lines)
        QMessageBox.information(self, "Route", text)

    def _send_exit(self):
        controller = self.controller
        source = self._selected_room_id()
        if not controller or source is None:
            QMessageBox.information(self, "Mapper", "Select a source room first.")
            return
        edge = self._choose_exit(source)
        if edge is None:
            QMessageBox.information(self, "Mapper", "That room has no exits.")
            return
        # Manual single-exit send deliberately does not auto-run pre-command;
        # prompt-aware sequencing belongs to the walker.
        result = controller.send_mapper_command(edge.command)
        if result.rejected_disconnected:
            QMessageBox.information(self, "Mapper", "Not connected.")

    def _walk_selected(self):
        controller = self.controller
        destination = self._selected_room_id()
        if not controller or destination is None:
            QMessageBox.information(self, "Mapper", "Select a destination room first.")
            return
        self._preview_route_steps = ()
        try:
            status = controller.start_mapper_walk(destination)
        except Exception as exc:
            QMessageBox.information(self, "Mapper Walk", str(exc))
            return
        if not status.active:
            QMessageBox.information(self, "Mapper Walk", status.message)

    def _stop_walking(self):
        if self.controller:
            self.controller.cancel_mapper_walk()
