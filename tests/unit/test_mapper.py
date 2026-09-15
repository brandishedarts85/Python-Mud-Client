from pathlib import Path
import pytest
from mapper import MapperRepository, Room


def test_room_exit_crud_and_cascade(tmp_path: Path):
    db = MapperRepository(tmp_path / 'map.sqlite3')
    a = db.add_room('A', area='Town', x=0, y=0, z=0)
    b = db.add_room('B', area='Town', x=0, y=1, z=0)
    edge = db.add_exit(a.id, b.id, 'north', command='n')
    assert db.exits_from(a.id) == (edge,)
    assert db.remove_room(b.id)
    assert db.exits_from(a.id) == ()
    db.close()


def test_duplicate_direction_rejected(tmp_path: Path):
    db = MapperRepository(tmp_path / 'map.sqlite3')
    a,b,c = [db.add_room(x) for x in 'ABC']
    db.add_exit(a.id,b.id,'north')
    with pytest.raises(ValueError): db.add_exit(a.id,c.id,'north')


def test_weighted_pathfinder_chooses_lower_cost(tmp_path: Path):
    db = MapperRepository(tmp_path / 'map.sqlite3')
    a,b,c,d = [db.add_room(x) for x in 'ABCD']
    db.add_exit(a.id,b.id,'n',cost=5)
    db.add_exit(b.id,d.id,'e',cost=5)
    db.add_exit(a.id,c.id,'s',cost=1)
    db.add_exit(c.id,d.id,'e',cost=1)
    route = db.find_route(a.id,d.id)
    assert [x.command for x in route] == ['s','e']


def test_unreachable_route_rejected(tmp_path: Path):
    db = MapperRepository(tmp_path / 'map.sqlite3')
    a,b = db.add_room('A'), db.add_room('B')
    with pytest.raises(ValueError, match='no route'): db.find_route(a.id,b.id)


def test_schema_persists(tmp_path: Path):
    path=tmp_path/'map.sqlite3'
    db=MapperRepository(path); room=db.add_room('Persistent'); db.close()
    db=MapperRepository(path); assert db.get_room(room.id).name=='Persistent'; db.close()


def test_observation_upserts_room_identity_and_resolvable_exits(tmp_path: Path):
    from mapper_adapter import RoomObservation, ObservedExit
    db = MapperRepository(tmp_path / 'map.sqlite3')
    room = db.apply_observation(RoomObservation('100', name='Start', area='Zone', exits=(ObservedExit('north','101','n'),), source='test'))
    assert room.external_id == '100'
    dest = db.find_room_by_external_id('101')
    assert dest is not None
    assert db.exits_from(room.id)[0].destination_room_id == dest.id
    room2 = db.apply_observation(RoomObservation('100', name='Start Renamed', source='test'))
    assert room2.id == room.id
    assert room2.name == 'Start Renamed'
    assert len(db.rooms()) == 2
    db.close()


def test_schema_v1_database_migrates_room_identity_columns(tmp_path: Path):
    import sqlite3
    path = tmp_path / 'old.sqlite3'
    conn = sqlite3.connect(path)
    conn.executescript("""
      CREATE TABLE mapper_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
      INSERT INTO mapper_meta VALUES('schema_version','1');
      CREATE TABLE rooms (id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,area TEXT NOT NULL DEFAULT '',x INTEGER,y INTEGER,z INTEGER,notes TEXT NOT NULL DEFAULT '');
      CREATE TABLE exits (id INTEGER PRIMARY KEY AUTOINCREMENT,source_room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,destination_room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,direction TEXT NOT NULL,command TEXT NOT NULL,cost REAL NOT NULL DEFAULT 1.0 CHECK(cost > 0),UNIQUE(source_room_id,direction));
      INSERT INTO rooms(name) VALUES('Legacy');
    """)
    conn.commit(); conn.close()
    db = MapperRepository(path)
    legacy = db.rooms()[0]
    assert legacy.name == 'Legacy' and legacy.external_id is None and legacy.source == 'manual'
    assert db._conn.execute("SELECT value FROM mapper_meta WHERE key='schema_version'").fetchone()[0] == '4'
    db.close()


def test_special_and_door_exit_metadata_round_trips_and_routes(tmp_path: Path):
    db = MapperRepository(tmp_path / 'map.sqlite3')
    a = db.add_room('A')
    b = db.add_room('B')
    c = db.add_room('C')
    door = db.add_exit(a.id, b.id, 'north', command='n', kind='door', pre_command='open north', cost=2)
    special = db.add_exit(b.id, c.id, 'portal', command='enter portal', kind='special', cost=1)
    assert door.kind == 'door' and door.pre_command == 'open north'
    assert special.kind == 'special' and special.pre_command is None
    route = db.find_route(a.id, c.id)
    assert [(step.kind, step.pre_command, step.command) for step in route] == [
        ('door', 'open north', 'n'),
        ('special', None, 'enter portal'),
    ]
    db.close()


def test_invalid_exit_kind_is_rejected(tmp_path: Path):
    db = MapperRepository(tmp_path / 'map.sqlite3')
    a, b = db.add_room('A'), db.add_room('B')
    with pytest.raises(ValueError, match='exit kind'):
        db.add_exit(a.id, b.id, 'north', kind='teleporter-ish')
    db.close()


def test_schema_v2_database_migrates_exit_kind_and_precommand(tmp_path: Path):
    import sqlite3
    path = tmp_path / 'old-v2.sqlite3'
    conn = sqlite3.connect(path)
    conn.executescript("""
      CREATE TABLE mapper_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
      INSERT INTO mapper_meta VALUES('schema_version','2');
      CREATE TABLE rooms (id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,area TEXT NOT NULL DEFAULT '',x INTEGER,y INTEGER,z INTEGER,notes TEXT NOT NULL DEFAULT '',external_id TEXT UNIQUE,source TEXT NOT NULL DEFAULT 'manual');
      CREATE TABLE exits (id INTEGER PRIMARY KEY AUTOINCREMENT,source_room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,destination_room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,direction TEXT NOT NULL,command TEXT NOT NULL,cost REAL NOT NULL DEFAULT 1.0 CHECK(cost > 0),UNIQUE(source_room_id,direction));
      INSERT INTO rooms(name) VALUES('A');
      INSERT INTO rooms(name) VALUES('B');
      INSERT INTO exits(source_room_id,destination_room_id,direction,command,cost) VALUES(1,2,'north','n',1.0);
    """)
    conn.commit(); conn.close()
    db = MapperRepository(path)
    edge = db.all_exits()[0]
    assert edge.kind == 'normal' and edge.pre_command is None
    assert db._conn.execute("SELECT value FROM mapper_meta WHERE key='schema_version'").fetchone()[0] == '4'
    db.close()


def test_room_observation_preserves_user_enriched_exit_metadata(tmp_path: Path):
    from mapper_adapter import RoomObservation, ObservedExit
    db = MapperRepository(tmp_path / 'map.sqlite3')
    source = db.apply_observation(
        RoomObservation('1', name='Start', exits=(ObservedExit('north', '2', 'north'),), source='generic-gmcp')
    )
    edge = db.exits_from(source.id)[0]
    db.update_exit(
        type(edge)(edge.id, edge.source_room_id, edge.destination_room_id, edge.direction,
                   'n', 2.5, 'door', 'open north')
    )
    # A later protocol refresh must not erase the user's richer movement model.
    db.apply_observation(
        RoomObservation('1', name='Start', exits=(ObservedExit('north', '2', 'north'),), source='generic-gmcp')
    )
    edge = db.exits_from(source.id)[0]
    assert (edge.command, edge.cost, edge.kind, edge.pre_command) == ('n', 2.5, 'door', 'open north')
    db.close()


def test_mapper6_route_preferences_avoid_area_and_tags(tmp_path: Path):
    from mapper import RoutePreferences
    db = MapperRepository(tmp_path / 'map.sqlite3')
    a = db.add_room('A', area='Start')
    bad = db.add_room('Bad Way', area='Swamp')
    good = db.add_room('Good Way', area='Road')
    d = db.add_room('D', area='End')
    db.add_exit(a.id, bad.id, 'north', cost=1, tags='risky')
    db.add_exit(bad.id, d.id, 'east', cost=1)
    db.add_exit(a.id, good.id, 'south', cost=2, tags='safe')
    db.add_exit(good.id, d.id, 'east', cost=2)
    assert [step.direction for step in db.find_route(a.id, d.id)] == ['north', 'east']
    assert [step.direction for step in db.find_route(a.id, d.id, RoutePreferences(avoid_areas=('Swamp',)))] == ['south', 'east']
    assert [step.direction for step in db.find_route(a.id, d.id, RoutePreferences(avoid_tags=('risky',)))] == ['south', 'east']
    db.close()


def test_mapper6_preferred_tags_adjust_effective_cost_without_changing_base_cost(tmp_path: Path):
    from mapper import RoutePreferences
    db = MapperRepository(tmp_path / 'map.sqlite3')
    a = db.add_room('A')
    b = db.add_room('B')
    c = db.add_room('C')
    d = db.add_room('D')
    db.add_exit(a.id, b.id, 'north', cost=1)
    db.add_exit(b.id, d.id, 'east', cost=1)
    db.add_exit(a.id, c.id, 'south', cost=1.5, tags='safe')
    db.add_exit(c.id, d.id, 'east', cost=1.5, tags='safe')
    prefs = RoutePreferences(prefer_tags=('safe',), prefer_multiplier=0.5)
    route = db.find_route(a.id, d.id, prefs)
    assert [step.direction for step in route] == ['south', 'east']
    assert [step.cost for step in route] == [1.5, 1.5]
    db.close()


def test_mapper6_route_preferences_and_layout_persist_with_profile_database(tmp_path: Path):
    from mapper import RoutePreferences
    path = tmp_path / 'map.sqlite3'
    db = MapperRepository(path)
    room = db.add_room('A', area='Zone')
    db.set_room_layout(room.id, 123.5, -44.25)
    db.set_route_preferences(RoutePreferences(
        avoid_areas=('Swamp',), avoid_tags=('risky',), prefer_tags=('safe', 'road'), prefer_multiplier=0.4
    ))
    db.close()
    db = MapperRepository(path)
    loaded = db.get_room(room.id)
    assert (loaded.layout_x, loaded.layout_y) == (123.5, -44.25)
    prefs = db.route_preferences
    assert prefs.avoid_areas == ('Swamp',)
    assert prefs.avoid_tags == ('risky',)
    assert prefs.prefer_tags == ('safe', 'road')
    assert prefs.prefer_multiplier == 0.4
    assert db.areas() == ('Zone',)
    db.close()


def test_mapper6_schema_v3_migrates_tags_and_layout_columns(tmp_path: Path):
    import sqlite3
    path = tmp_path / 'old-v3.sqlite3'
    conn = sqlite3.connect(path)
    conn.executescript("""
      CREATE TABLE mapper_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
      INSERT INTO mapper_meta VALUES('schema_version','3');
      CREATE TABLE rooms (id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,area TEXT NOT NULL DEFAULT '',x INTEGER,y INTEGER,z INTEGER,notes TEXT NOT NULL DEFAULT '',external_id TEXT UNIQUE,source TEXT NOT NULL DEFAULT 'manual');
      CREATE TABLE exits (id INTEGER PRIMARY KEY AUTOINCREMENT,source_room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,destination_room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,direction TEXT NOT NULL,command TEXT NOT NULL,cost REAL NOT NULL DEFAULT 1.0 CHECK(cost > 0),kind TEXT NOT NULL DEFAULT 'normal',pre_command TEXT,UNIQUE(source_room_id,direction));
      INSERT INTO rooms(name) VALUES('A'); INSERT INTO rooms(name) VALUES('B');
      INSERT INTO exits(source_room_id,destination_room_id,direction,command,cost,kind) VALUES(1,2,'north','n',1.0,'normal');
    """)
    conn.commit(); conn.close()
    db = MapperRepository(path)
    room = db.get_room(1); edge = db.all_exits()[0]
    assert room.layout_x is None and room.layout_y is None and edge.tags == ''
    assert db._conn.execute("SELECT value FROM mapper_meta WHERE key='schema_version'").fetchone()[0] == '4'
    db.close()
