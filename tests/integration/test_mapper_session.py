from pathlib import Path
from session_controller import MudSessionController
from command_pipeline import CommandSource


def test_mapper_scope_switches_with_profile(tmp_path: Path):
    automation = tmp_path/'automation.json'
    c = MudSessionController(automation_path=str(automation), mapper_path=str(tmp_path/'mapper.sqlite3'))
    global_path = c.mapper_path
    c.mapper.add_room('Global Room')
    c.set_automation_scope('Hero')
    assert c.mapper_path != global_path
    assert c.mapper.rooms() == ()
    c.mapper.add_room('Hero Room')
    c.set_automation_scope(None)
    assert [r.name for r in c.mapper.rooms()] == ['Global Room']
    c.mapper.close()


def test_mapper_command_uses_mapper_source(monkeypatch, tmp_path: Path):
    c = MudSessionController(automation_path=str(tmp_path/'automation.json'), mapper_path=str(tmp_path/'map.sqlite3'))
    captured=[]
    monkeypatch.setattr(c,'dispatch_command',lambda req: captured.append(req) or object())
    c.send_mapper_command('north')
    assert captured[0].source is CommandSource.MAPPER
    assert captured[0].text == 'north'
    c.mapper.close()


def test_gmcp_room_info_updates_mapper_through_adapter(tmp_path: Path):
    c = MudSessionController(automation_path=str(tmp_path/'automation.json'), mapper_path=str(tmp_path/'map.sqlite3'))
    c._on_gmcp(c._connection_generation, {'package':'Room.Info','data':{'num':77,'name':'Observed Room','area':'Test','exits':{'east':78}}})
    room = c.mapper.find_room_by_external_id('77')
    assert room is not None and room.name == 'Observed Room'
    assert c.current_room_id == room.id
    assert c.mapper.find_room_by_external_id('78') is not None
    c.mapper.close()


def test_manual_mapper_adapter_disables_protocol_discovery(tmp_path: Path):
    c = MudSessionController(automation_path=str(tmp_path/'automation.json'), mapper_path=str(tmp_path/'map.sqlite3'))
    c.set_mapper_adapter('manual')
    c._on_gmcp(c._connection_generation, {'package':'Room.Info','data':{'num':77,'name':'Ignored'}})
    assert c.mapper.rooms() == ()
    assert c.current_room_id is None
    c.mapper.close()


def test_prompt_aware_walk_advances_only_after_expected_room_and_prompt(monkeypatch, tmp_path: Path):
    c = MudSessionController(automation_path=str(tmp_path/'automation.json'), mapper_path=str(tmp_path/'map.sqlite3'))
    a=c.mapper.add_room('A', external_id='1', source='generic-gmcp')
    b=c.mapper.add_room('B', external_id='2', source='generic-gmcp')
    d=c.mapper.add_room('D', external_id='3', source='generic-gmcp')
    c.mapper.add_exit(a.id,b.id,'north')
    c.mapper.add_exit(b.id,d.id,'east')
    c.current_room_id=a.id
    sent=[]
    monkeypatch.setattr(c,'send_mapper_command',lambda cmd: type('R',(),{'sent':(cmd,), 'rejected_disconnected':False})())
    c.start_mapper_walk(d.id)
    assert c.mapper_walk_status.active
    # Walker captured the controller helper, so patch its sender for this isolated test.
    c.mapper_walker._send_command=lambda cmd: sent.append(cmd) or True
    c.cancel_mapper_walk()
    c.start_mapper_walk(d.id)
    assert sent == ['north']
    c.mapper_walker.note_prompt(); assert sent == ['north']
    c.mapper_walker.note_room(b.id); assert sent == ['north','east']
    c.mapper_walker.note_room(d.id); assert c.mapper_walk_status.active
    c.mapper_walker.note_prompt(); assert c.mapper_walk_status.message == 'arrived'
    c.mapper.close()


def test_auto_walk_refuses_manual_only_or_unidentified_destinations(tmp_path: Path):
    c = MudSessionController(automation_path=str(tmp_path/'automation.json'), mapper_path=str(tmp_path/'map.sqlite3'))
    a=c.mapper.add_room('A', external_id='1', source='generic-gmcp')
    b=c.mapper.add_room('B')
    c.mapper.add_exit(a.id,b.id,'north')
    c.current_room_id=a.id
    c.set_mapper_adapter('manual')
    import pytest
    with pytest.raises(ValueError, match='room-identity adapter'):
        c.start_mapper_walk(b.id)
    c.set_mapper_adapter('generic-gmcp')
    with pytest.raises(ValueError, match='stable external identity'):
        c.start_mapper_walk(b.id)
    c.mapper.close()


def test_mapper_walk_is_cancelled_on_disconnect_and_adapter_change(tmp_path: Path):
    c = MudSessionController(automation_path=str(tmp_path/'automation.json'), mapper_path=str(tmp_path/'map.sqlite3'))
    a=c.mapper.add_room('A', external_id='1', source='generic-gmcp')
    b=c.mapper.add_room('B', external_id='2', source='generic-gmcp')
    c.mapper.add_exit(a.id,b.id,'north')
    c.current_room_id=a.id
    c.mapper_walker._send_command=lambda _cmd: True
    c.start_mapper_walk(b.id)
    assert c.mapper_walk_status.active
    c.set_mapper_adapter('manual')
    assert not c.mapper_walk_status.active
    c.set_mapper_adapter('generic-gmcp')
    c.current_room_id=a.id
    c.start_mapper_walk(b.id)
    c._on_disconnected(c._connection_generation)
    assert not c.mapper_walk_status.active
    assert 'disconnected' in c.mapper_walk_status.message
    c.mapper.close()


def test_controller_controlled_reroute_uses_observed_room_and_same_destination(tmp_path: Path):
    c = MudSessionController(automation_path=str(tmp_path/'automation.json'), mapper_path=str(tmp_path/'map.sqlite3'))
    a=c.mapper.add_room('A', external_id='1', source='generic-gmcp')
    b=c.mapper.add_room('B', external_id='2', source='generic-gmcp')
    x=c.mapper.add_room('X', external_id='9', source='generic-gmcp')
    d=c.mapper.add_room('D', external_id='4', source='generic-gmcp')
    c.mapper.add_exit(a.id,b.id,'north')
    c.mapper.add_exit(b.id,d.id,'east')
    c.mapper.add_exit(x.id,d.id,'south')
    c.current_room_id=a.id
    sent=[]
    c.mapper_walker._send_command=lambda cmd: sent.append(cmd) or True
    c.start_mapper_walk(d.id)
    assert sent == ['north']
    # Stable observation says the move landed in X instead of B.
    c.current_room_id=x.id
    c.mapper_walker.note_room(x.id)
    assert c.mapper_walk_status.active
    assert c.mapper_walk_status.phase == 'reroute_wait'
    assert sent == ['north']
    c.mapper_walker.note_prompt()
    assert sent == ['north','south']
    c.mapper_walker.note_room(d.id); c.mapper_walker.note_prompt()
    assert c.mapper_walk_status.message == 'arrived'
    c.mapper.close()


def test_controller_reroute_refuses_unidentified_replacement_rooms(tmp_path: Path):
    c = MudSessionController(automation_path=str(tmp_path/'automation.json'), mapper_path=str(tmp_path/'map.sqlite3'))
    a=c.mapper.add_room('A', external_id='1', source='generic-gmcp')
    b=c.mapper.add_room('B', external_id='2', source='generic-gmcp')
    x=c.mapper.add_room('X', external_id='9', source='generic-gmcp')
    unknown=c.mapper.add_room('Unknown')
    d=c.mapper.add_room('D', external_id='4', source='generic-gmcp')
    c.mapper.add_exit(a.id,b.id,'north')
    c.mapper.add_exit(b.id,d.id,'east')
    c.mapper.add_exit(x.id,unknown.id,'south')
    c.mapper.add_exit(unknown.id,d.id,'east')
    c.current_room_id=a.id
    c.mapper_walker._send_command=lambda _cmd: True
    c.start_mapper_walk(d.id)
    c.mapper_walker.note_room(x.id)
    assert not c.mapper_walk_status.active
    assert 'no safe reroute' in c.mapper_walk_status.message
    c.mapper.close()


def test_mg_room_adapter_updates_mapper_from_evidenced_package(tmp_path: Path):
    c = MudSessionController(automation_path=str(tmp_path/'automation.json'), mapper_path=str(tmp_path/'map.sqlite3'))
    c.set_mapper_adapter('mg-room')
    assert c.current_room_id is None
    c._on_gmcp(c._connection_generation, {
        'package':'MG.room.info',
        'data':{'id':'5833','short':'Tower of Light','area':'Manetheren','exits':{'up':6306}},
    })
    room = c.mapper.find_room_by_external_id('5833')
    assert room is not None and room.name == 'Tower of Light' and room.area == 'Manetheren'
    assert c.current_room_id == room.id
    assert c.mapper.find_room_by_external_id('6306') is not None
    c.mapper.close()


def test_controller_uses_adapter_capabilities_not_manual_key(tmp_path: Path):
    from mapper_adapter import MapperAdapterCapability
    c = MudSessionController(automation_path=str(tmp_path/'automation.json'), mapper_path=str(tmp_path/'map.sqlite3'))
    c.set_mapper_adapter('manual')
    assert not c.mapper_adapter_supports(MapperAdapterCapability.ROOM_IDENTITY)
    assert not c.mapper_adapter_supports(MapperAdapterCapability.GMCP)
    c.set_mapper_adapter('mg-room')
    assert c.mapper_adapter_supports(MapperAdapterCapability.ROOM_IDENTITY)
    assert c.mapper_adapter_supports(MapperAdapterCapability.GMCP)
    assert not c.mapper_adapter_supports(MapperAdapterCapability.MSDP)
    c.mapper.close()


def test_mapper_walk_and_reroute_use_persisted_route_preferences(tmp_path: Path):
    from mapper import RoutePreferences
    c = MudSessionController(automation_path=str(tmp_path/'automation.json'), mapper_path=str(tmp_path/'map.sqlite3'))
    a=c.mapper.add_room('A', external_id='1', source='generic-gmcp')
    risky=c.mapper.add_room('Risky', external_id='2', source='generic-gmcp')
    safe=c.mapper.add_room('Safe', external_id='3', source='generic-gmcp')
    d=c.mapper.add_room('D', external_id='4', source='generic-gmcp')
    c.mapper.add_exit(a.id,risky.id,'north',cost=1,tags='risky')
    c.mapper.add_exit(risky.id,d.id,'east',cost=1)
    c.mapper.add_exit(a.id,safe.id,'south',cost=2,tags='safe')
    c.mapper.add_exit(safe.id,d.id,'east',cost=2,tags='safe')
    c.mapper.set_route_preferences(RoutePreferences(avoid_tags=('risky',)))
    c.current_room_id=a.id
    sent=[]
    c.mapper_walker._send_command=lambda cmd: sent.append(cmd) or True
    c.start_mapper_walk(d.id)
    assert sent == ['south']
    c.cancel_mapper_walk()
    assert [step.direction for step in c._mapper_reroute(a.id,d.id)] == ['south','east']
    c.mapper.close()
