from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]

def test_mapper_dock_is_wired_into_main_window():
    text=(ROOT/'qt/main_window.py').read_text()
    assert 'MapperDock' in text
    assert 'self.mapper_dock.set_controller(controller)' in text

def test_mapper_send_uses_command_boundary():
    controller=(ROOT/'session_controller.py').read_text()
    dock=(ROOT/'qt/docks/mapper.py').read_text()
    assert 'CommandSource.MAPPER' in controller
    assert 'send_mapper_command' in dock
    assert '.conn.send_line(' not in dock

def test_mapper_dock_exposes_adapter_selection_without_protocol_parsing():
    dock=(ROOT/'qt/docks/mapper.py').read_text()
    assert 'mapper_adapter_choices' in dock
    assert 'set_mapper_adapter' in dock
    assert 'Room.Info' not in dock


def test_mapper_dock_exposes_prompt_aware_walk_controls():
    dock=(ROOT/'qt/docks/mapper.py').read_text()
    controller=(ROOT/'session_controller.py').read_text()
    assert 'Walk to Selected' in dock
    assert 'start_mapper_walk' in dock
    assert 'cancel_mapper_walk' in dock
    assert 'mapper_walker.note_prompt()' in controller
    assert 'mapper_walker.note_room(room.id)' in controller
    assert '.conn.send_line(' not in dock


def test_mapper_dock_exposes_special_exit_metadata_and_editing():
    dock=(ROOT/'qt/docks/mapper.py').read_text()
    assert 'Edit Exit' in dock
    assert 'EXIT_KINDS' in dock
    assert 'Pre-command' in dock
    assert 'update_exit' in dock


def test_mapper_5_graphical_view_is_read_only_and_wired_into_dock():
    dock=(ROOT/'qt/docks/mapper.py').read_text()
    view=(ROOT/'qt/map_view.py').read_text()
    assert 'MapperGraphicsView' in dock
    assert 'QGraphicsView' in view
    assert 'ScrollHandDrag' in view
    assert 'wheelEvent' in view
    assert 'fit_map' in view
    assert 'Center Current' in dock
    assert '.conn.send_line(' not in view
    assert 'MudSessionController' not in view


def test_mapper_5_visual_highlights_use_active_route_snapshot():
    dock=(ROOT/'qt/docks/mapper.py').read_text()
    walker=(ROOT/'mapper_walker.py').read_text()
    assert 'route_steps' in walker
    assert 'controller.mapper_walker.route_steps' in dock
    assert 'route_exit_ids' in (ROOT/'qt/map_view.py').read_text()


def test_mapper_5_supports_manual_coordinate_editing():
    dock=(ROOT/'qt/docks/mapper.py').read_text()
    assert 'Edit Room' in dock
    assert 'X coordinate' in dock
    assert 'controller.mapper.update_room' in dock


def test_root_roadmap_remains_release_artifact():
    roadmap=ROOT/'ROADMAP.md'
    assert roadmap.exists()
    assert 'Mapper 6' in roadmap.read_text()


def test_mapper_6_exposes_route_policy_area_filter_and_manual_layout():
    dock=(ROOT/'qt/docks/mapper.py').read_text()
    model=(ROOT/'mapper.py').read_text()
    view=(ROOT/'qt/map_view.py').read_text()
    assert 'Route Preferences' in dock
    assert 'View area:' in dock
    assert 'Route tags' in dock
    assert 'set_room_layout' in dock
    assert 'avoid_areas' in model and 'avoid_tags' in model and 'prefer_tags' in model
    assert 'on_room_moved' in view
    assert '.conn.send_line(' not in view
