"""Deterministic UI-neutral room positioning for the graphical mapper.

Explicit room coordinates remain authoritative. Rooms without coordinates are
placed on a deterministic fallback grid so the visual map is useful before a
MUD supplies spatial metadata or the user edits coordinates manually.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from mapper import Room

DEFAULT_GRID_SPACING = 140.0
DEFAULT_FLOOR_OFFSET = 28.0


@dataclass(frozen=True)
class RoomPosition:
    room_id: int
    x: float
    y: float
    z: int
    explicit: bool


def compute_room_positions(
    rooms: Iterable[Room],
    *,
    grid_spacing: float = DEFAULT_GRID_SPACING,
    floor_offset: float = DEFAULT_FLOOR_OFFSET,
) -> dict[int, RoomPosition]:
    """Return deterministic scene positions keyed by room id.

    Persisted manual ``layout_x``/``layout_y`` positions take precedence. Otherwise
    ``x``/``y`` room coordinates, when both are known, are treated as map-grid
    coordinates. Qt's Y axis points downward, so mapper Y is inverted. Z floors
    receive a small diagonal offset so identical coordinates on different
    floors remain visually distinguishable without fabricating connectivity.

    Rooms lacking either X or Y are assigned fallback grid cells in stable
    ``(area, name, id)`` order. The fallback region starts to the right of all
    explicit positions to avoid covering trusted coordinates.
    """
    rooms = tuple(rooms)
    if grid_spacing <= 0:
        raise ValueError("grid spacing must be > 0")
    if floor_offset < 0:
        raise ValueError("floor offset must be >= 0")

    result: dict[int, RoomPosition] = {}
    explicit_xs: list[float] = []
    fallback: list[Room] = []

    for room in rooms:
        z = 0 if room.z is None else int(room.z)
        if room.layout_x is not None and room.layout_y is not None:
            x = float(room.layout_x)
            y = float(room.layout_y)
            result[room.id] = RoomPosition(room.id, x, y, z, True)
            explicit_xs.append(x)
            continue
        if room.x is None or room.y is None:
            fallback.append(room)
            continue
        x = float(room.x) * grid_spacing + z * floor_offset
        y = -float(room.y) * grid_spacing + z * floor_offset
        result[room.id] = RoomPosition(room.id, x, y, z, True)
        explicit_xs.append(x)

    start_x = (max(explicit_xs) + grid_spacing * 1.5) if explicit_xs else 0.0
    ordered = sorted(fallback, key=lambda room: (room.area.casefold(), room.name.casefold(), room.id))
    columns = max(1, min(8, int(len(ordered) ** 0.5) + 1))
    for index, room in enumerate(ordered):
        column = index % columns
        row = index // columns
        z = 0 if room.z is None else int(room.z)
        x = start_x + column * grid_spacing + z * floor_offset
        y = row * grid_spacing + z * floor_offset
        result[room.id] = RoomPosition(room.id, x, y, z, False)

    return result
