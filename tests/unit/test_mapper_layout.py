from mapper import Room
from mapper_layout import compute_room_positions


def _room(room_id, name, *, area='', x=None, y=None, z=None):
    return Room(room_id, name, area, x, y, z)


def test_explicit_coordinates_are_authoritative_and_y_is_inverted():
    positions = compute_room_positions([_room(1, 'A', x=2, y=3, z=0)], grid_spacing=100)
    point = positions[1]
    assert (point.x, point.y, point.z, point.explicit) == (200.0, -300.0, 0, True)


def test_z_floor_offsets_overlapping_explicit_rooms():
    positions = compute_room_positions(
        [_room(1, 'Ground', x=0, y=0, z=0), _room(2, 'Upper', x=0, y=0, z=1)],
        grid_spacing=100,
        floor_offset=25,
    )
    assert (positions[1].x, positions[1].y) == (0.0, 0.0)
    assert (positions[2].x, positions[2].y) == (25.0, 25.0)


def test_unpositioned_rooms_use_deterministic_fallback_grid():
    rooms = [_room(3, 'C', area='B'), _room(1, 'A', area='A'), _room(2, 'B', area='A')]
    first = compute_room_positions(rooms, grid_spacing=100)
    second = compute_room_positions(reversed(rooms), grid_spacing=100)
    assert first == second
    assert all(not point.explicit for point in first.values())
    assert len({(point.x, point.y) for point in first.values()}) == 3


def test_fallback_rooms_are_placed_to_right_of_explicit_region():
    rooms = [_room(1, 'Pinned', x=4, y=0), _room(2, 'Auto')]
    positions = compute_room_positions(rooms, grid_spacing=100)
    assert positions[2].x > positions[1].x


def test_invalid_layout_spacing_rejected():
    import pytest
    with pytest.raises(ValueError, match='grid spacing'):
        compute_room_positions([], grid_spacing=0)


def test_manual_layout_overrides_world_coordinates_and_fallback():
    from mapper import Room
    room = Room(1, 'A', x=9, y=9, z=0, layout_x=321.5, layout_y=-88.0)
    pos = compute_room_positions((room,))[1]
    assert (pos.x, pos.y) == (321.5, -88.0)
