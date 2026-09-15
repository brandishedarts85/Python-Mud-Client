"""SQLite-backed generic mapper model and pathfinding.

The mapper is deliberately UI- and MUD-independent. Rooms and directed exits are
stored in SQLite; callers decide how room identity is discovered and when a
movement command is safe to send.
"""
from __future__ import annotations

import heapq
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from mapper_adapter import RoomObservation

SCHEMA_VERSION = 4
MAX_ROOM_NAME = 512
MAX_AREA_NAME = 256
MAX_EXIT_COMMAND = 512
EXIT_KINDS = ("normal", "door", "special")
MAX_ROUTE_TAG = 64
MAX_ROUTE_TAGS = 32


@dataclass(frozen=True)
class Room:
    id: int
    name: str
    area: str = ""
    x: int | None = None
    y: int | None = None
    z: int | None = None
    notes: str = ""
    external_id: str | None = None
    source: str = "manual"
    layout_x: float | None = None
    layout_y: float | None = None


@dataclass(frozen=True)
class Exit:
    id: int
    source_room_id: int
    destination_room_id: int
    direction: str
    command: str
    cost: float = 1.0
    kind: str = "normal"
    pre_command: str | None = None
    tags: str = ""


@dataclass(frozen=True)
class RouteStep:
    exit_id: int
    source_room_id: int
    destination_room_id: int
    direction: str
    command: str
    cost: float
    kind: str = "normal"
    pre_command: str | None = None
    tags: str = ""


@dataclass(frozen=True)
class RoutePreferences:
    """Profile-scoped routing policy stored with the mapper database.

    Avoided areas and tags are hard constraints. Preferred tags reduce an
    edge's effective pathfinding cost without changing the edge's persisted
    base cost. Source and destination rooms are never excluded solely because
    their area is in ``avoid_areas``.
    """

    avoid_areas: tuple[str, ...] = ()
    avoid_tags: tuple[str, ...] = ()
    prefer_tags: tuple[str, ...] = ()
    prefer_multiplier: float = 0.5


class MapperRepository:
    def __init__(self, path: str | Path = "mapper.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS mapper_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rooms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    area TEXT NOT NULL DEFAULT '',
                    x INTEGER,
                    y INTEGER,
                    z INTEGER,
                    notes TEXT NOT NULL DEFAULT '',
                    external_id TEXT UNIQUE,
                    source TEXT NOT NULL DEFAULT 'manual',
                    layout_x REAL,
                    layout_y REAL
                );
                CREATE TABLE IF NOT EXISTS exits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
                    destination_room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
                    direction TEXT NOT NULL,
                    command TEXT NOT NULL,
                    cost REAL NOT NULL DEFAULT 1.0 CHECK(cost > 0),
                    kind TEXT NOT NULL DEFAULT 'normal',
                    pre_command TEXT,
                    tags TEXT NOT NULL DEFAULT '',
                    UNIQUE(source_room_id, direction)
                );
                CREATE INDEX IF NOT EXISTS idx_exits_source ON exits(source_room_id);
                CREATE INDEX IF NOT EXISTS idx_exits_destination ON exits(destination_room_id);
                """
            )
            current = self._conn.execute(
                "SELECT value FROM mapper_meta WHERE key='schema_version'"
            ).fetchone()
            if current is None:
                self._conn.execute(
                    "INSERT INTO mapper_meta(key,value) VALUES('schema_version',?)",
                    (str(SCHEMA_VERSION),),
                )
                return

            version = int(current["value"])
            if version > SCHEMA_VERSION:
                raise ValueError(
                    f"mapper database schema {current['value']} is newer than supported {SCHEMA_VERSION}"
                )
            if version < 2:
                columns = {row[1] for row in self._conn.execute("PRAGMA table_info(rooms)")}
                if "external_id" not in columns:
                    self._conn.execute("ALTER TABLE rooms ADD COLUMN external_id TEXT")
                if "source" not in columns:
                    self._conn.execute(
                        "ALTER TABLE rooms ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'"
                    )
                self._conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_rooms_external_id "
                    "ON rooms(external_id) WHERE external_id IS NOT NULL"
                )
                version = 2
                self._conn.execute(
                    "UPDATE mapper_meta SET value='2' WHERE key='schema_version'"
                )
            if version < 3:
                columns = {row[1] for row in self._conn.execute("PRAGMA table_info(exits)")}
                if "kind" not in columns:
                    self._conn.execute(
                        "ALTER TABLE exits ADD COLUMN kind TEXT NOT NULL DEFAULT 'normal'"
                    )
                if "pre_command" not in columns:
                    self._conn.execute("ALTER TABLE exits ADD COLUMN pre_command TEXT")
                self._conn.execute(
                    "UPDATE mapper_meta SET value='3' WHERE key='schema_version'"
                )
            if version < 4:
                room_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(rooms)")}
                if "layout_x" not in room_columns:
                    self._conn.execute("ALTER TABLE rooms ADD COLUMN layout_x REAL")
                if "layout_y" not in room_columns:
                    self._conn.execute("ALTER TABLE rooms ADD COLUMN layout_y REAL")
                exit_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(exits)")}
                if "tags" not in exit_columns:
                    self._conn.execute("ALTER TABLE exits ADD COLUMN tags TEXT NOT NULL DEFAULT ''")
                self._conn.execute(
                    "UPDATE mapper_meta SET value='4' WHERE key='schema_version'"
                )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "MapperRepository":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    @staticmethod
    def _clean_text(value: str, *, field: str, max_len: int, required: bool = False) -> str:
        text = str(value).strip()
        if required and not text:
            raise ValueError(f"{field} must not be empty")
        if len(text) > max_len:
            raise ValueError(f"{field} exceeds {max_len} characters")
        return text

    @staticmethod
    def _clean_optional_command(value: str | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        if len(text) > MAX_EXIT_COMMAND:
            raise ValueError(f"pre-command exceeds {MAX_EXIT_COMMAND} characters")
        return text

    @staticmethod
    def _clean_exit_kind(kind: str) -> str:
        normalized = str(kind or "normal").strip().casefold()
        if normalized not in EXIT_KINDS:
            raise ValueError(f"exit kind must be one of: {', '.join(EXIT_KINDS)}")
        return normalized

    @staticmethod
    def _clean_tags(value) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            raw = value.replace(",", " ").split()
        else:
            raw = [str(item) for item in value]
        normalized: list[str] = []
        seen: set[str] = set()
        for item in raw:
            tag = str(item).strip().casefold()
            if not tag:
                continue
            if len(tag) > MAX_ROUTE_TAG:
                raise ValueError(f"route tag exceeds {MAX_ROUTE_TAG} characters")
            if tag not in seen:
                seen.add(tag)
                normalized.append(tag)
            if len(normalized) > MAX_ROUTE_TAGS:
                raise ValueError(f"an exit may have at most {MAX_ROUTE_TAGS} route tags")
        return " ".join(normalized)

    @staticmethod
    def _clean_name_list(values) -> tuple[str, ...]:
        if isinstance(values, str):
            values = values.replace(",", " ").split()
        out: list[str] = []
        seen: set[str] = set()
        for value in values or ():
            text = str(value).strip()
            if not text:
                continue
            key = text.casefold()
            if key not in seen:
                seen.add(key)
                out.append(text)
        return tuple(out)

    @staticmethod
    def _meta_key(name: str) -> str:
        return f"route_preferences.{name}"

    @property
    def route_preferences(self) -> RoutePreferences:
        raw = self._conn.execute(
            "SELECT key,value FROM mapper_meta WHERE key LIKE 'route_preferences.%'"
        ).fetchall()
        data = {str(row["key"]).split(".", 1)[1]: row["value"] for row in raw}
        def load_list(name: str) -> tuple[str, ...]:
            try:
                value = json.loads(data.get(name, "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                return ()
            return self._clean_name_list(value if isinstance(value, list) else ())
        try:
            multiplier = float(data.get("prefer_multiplier", "0.5"))
        except (TypeError, ValueError):
            multiplier = 0.5
        if not (0 < multiplier <= 1):
            multiplier = 0.5
        return RoutePreferences(
            avoid_areas=load_list("avoid_areas"),
            avoid_tags=tuple(v.casefold() for v in load_list("avoid_tags")),
            prefer_tags=tuple(v.casefold() for v in load_list("prefer_tags")),
            prefer_multiplier=multiplier,
        )

    def set_route_preferences(self, preferences: RoutePreferences) -> RoutePreferences:
        avoid_areas = self._clean_name_list(preferences.avoid_areas)
        avoid_tags = tuple(self._clean_tags(preferences.avoid_tags).split())
        prefer_tags = tuple(self._clean_tags(preferences.prefer_tags).split())
        multiplier = float(preferences.prefer_multiplier)
        if not (0 < multiplier <= 1):
            raise ValueError("preferred-route multiplier must be > 0 and <= 1")
        values = {
            "avoid_areas": json.dumps(avoid_areas, ensure_ascii=False),
            "avoid_tags": json.dumps(avoid_tags, ensure_ascii=False),
            "prefer_tags": json.dumps(prefer_tags, ensure_ascii=False),
            "prefer_multiplier": repr(multiplier),
        }
        with self._conn:
            for name, value in values.items():
                self._conn.execute(
                    "INSERT INTO mapper_meta(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (self._meta_key(name), value),
                )
        return self.route_preferences

    def areas(self) -> tuple[str, ...]:
        rows = self._conn.execute(
            "SELECT DISTINCT area FROM rooms WHERE TRIM(area)<>'' ORDER BY area COLLATE NOCASE"
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def set_room_layout(self, room_id: int, x: float | None, y: float | None) -> Room:
        self.get_room(room_id)
        if (x is None) != (y is None):
            raise ValueError("layout X and Y must both be set or both be cleared")
        with self._conn:
            self._conn.execute(
                "UPDATE rooms SET layout_x=?,layout_y=? WHERE id=?",
                (None if x is None else float(x), None if y is None else float(y), int(room_id)),
            )
        return self.get_room(room_id)

    def add_room(
        self,
        name: str,
        *,
        area: str = "",
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
        notes: str = "",
        external_id: str | None = None,
        source: str = "manual",
        layout_x: float | None = None,
        layout_y: float | None = None,
    ) -> Room:
        name = self._clean_text(name, field="room name", max_len=MAX_ROOM_NAME, required=True)
        area = self._clean_text(area, field="area", max_len=MAX_AREA_NAME)
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO rooms(name,area,x,y,z,notes,external_id,source,layout_x,layout_y) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (name, area, x, y, z, str(notes), external_id, str(source), layout_x, layout_y),
            )
        return self.get_room(int(cur.lastrowid))

    def update_room(self, room: Room) -> Room:
        name = self._clean_text(room.name, field="room name", max_len=MAX_ROOM_NAME, required=True)
        area = self._clean_text(room.area, field="area", max_len=MAX_AREA_NAME)
        with self._conn:
            cur = self._conn.execute(
                "UPDATE rooms SET name=?,area=?,x=?,y=?,z=?,notes=?,external_id=?,source=?,layout_x=?,layout_y=? WHERE id=?",
                (
                    name,
                    area,
                    room.x,
                    room.y,
                    room.z,
                    room.notes,
                    room.external_id,
                    room.source,
                    room.layout_x,
                    room.layout_y,
                    room.id,
                ),
            )
        if cur.rowcount != 1:
            raise KeyError(room.id)
        return self.get_room(room.id)

    def remove_room(self, room_id: int) -> bool:
        with self._conn:
            cur = self._conn.execute("DELETE FROM rooms WHERE id=?", (int(room_id),))
        return cur.rowcount == 1

    def get_room(self, room_id: int) -> Room:
        row = self._conn.execute("SELECT * FROM rooms WHERE id=?", (int(room_id),)).fetchone()
        if row is None:
            raise KeyError(room_id)
        return Room(**dict(row))

    def rooms(self) -> tuple[Room, ...]:
        rows = self._conn.execute("SELECT * FROM rooms ORDER BY area,name,id").fetchall()
        return tuple(Room(**dict(row)) for row in rows)

    def find_room_by_external_id(self, external_id: str) -> Room | None:
        row = self._conn.execute(
            "SELECT * FROM rooms WHERE external_id=?", (str(external_id),)
        ).fetchone()
        return None if row is None else Room(**dict(row))

    def apply_observation(self, observation: RoomObservation) -> Room:
        """Upsert one normalized room observation and any resolvable exits."""
        external_id = self._clean_text(
            observation.external_id,
            field="external room id",
            max_len=512,
            required=True,
        )
        room = self.find_room_by_external_id(external_id)
        if room is None:
            room = self.add_room(
                observation.name or f"[{external_id}]",
                area=observation.area or "",
                x=observation.x,
                y=observation.y,
                z=observation.z,
                external_id=external_id,
                source=observation.source,
            )
        else:
            room = self.update_room(
                Room(
                    room.id,
                    observation.name or room.name,
                    observation.area if observation.area is not None else room.area,
                    observation.x if observation.x is not None else room.x,
                    observation.y if observation.y is not None else room.y,
                    observation.z if observation.z is not None else room.z,
                    room.notes,
                    room.external_id,
                    observation.source or room.source,
                    room.layout_x,
                    room.layout_y,
                )
            )
        for observed in observation.exits:
            if not observed.destination_external_id:
                continue
            dest = self.find_room_by_external_id(observed.destination_external_id)
            if dest is None:
                dest = self.add_room(
                    f"[{observed.destination_external_id}]",
                    external_id=observed.destination_external_id,
                    source=observation.source,
                )
            # Protocol discovery only knows ordinary movement unless a
            # MUD-specific adapter later supplies richer semantics. If the user
            # has already enriched this edge (door/special type, pre-command,
            # custom command/cost), preserve that local knowledge while still
            # accepting a newly observed destination.
            existing_row = self._conn.execute(
                "SELECT id FROM exits WHERE source_room_id=? AND direction=?",
                (room.id, str(observed.direction).strip()),
            ).fetchone()
            if existing_row is None:
                self.add_exit(
                    room.id,
                    dest.id,
                    observed.direction,
                    command=observed.command or observed.direction,
                    cost=observed.cost,
                    kind="normal",
                )
            else:
                existing = self.get_exit(int(existing_row["id"]))
                self.update_exit(
                    Exit(
                        existing.id,
                        existing.source_room_id,
                        dest.id,
                        existing.direction,
                        existing.command,
                        existing.cost,
                        existing.kind,
                        existing.pre_command,
                        existing.tags,
                    )
                )
        return room

    def upsert_exit(
        self,
        source_room_id: int,
        destination_room_id: int,
        direction: str,
        *,
        command: str | None = None,
        cost: float = 1.0,
        kind: str = "normal",
        pre_command: str | None = None,
        tags: str | tuple[str, ...] = "",
    ) -> Exit:
        direction = self._clean_text(
            direction, field="direction", max_len=MAX_EXIT_COMMAND, required=True
        )
        existing = self._conn.execute(
            "SELECT id FROM exits WHERE source_room_id=? AND direction=?",
            (int(source_room_id), direction),
        ).fetchone()
        if existing is None:
            return self.add_exit(
                source_room_id,
                destination_room_id,
                direction,
                command=command,
                cost=cost,
                kind=kind,
                pre_command=pre_command,
                tags=tags,
            )
        command = self._clean_text(
            command or direction, field="command", max_len=MAX_EXIT_COMMAND, required=True
        )
        cost = float(cost)
        if not (cost > 0):
            raise ValueError("exit cost must be > 0")
        kind = self._clean_exit_kind(kind)
        pre_command = self._clean_optional_command(pre_command)
        tags = self._clean_tags(tags)
        with self._conn:
            self._conn.execute(
                "UPDATE exits SET destination_room_id=?,command=?,cost=?,kind=?,pre_command=?,tags=? WHERE id=?",
                (
                    int(destination_room_id),
                    command,
                    cost,
                    kind,
                    pre_command,
                    tags,
                    int(existing["id"]),
                ),
            )
        return self.get_exit(int(existing["id"]))

    def add_exit(
        self,
        source_room_id: int,
        destination_room_id: int,
        direction: str,
        *,
        command: str | None = None,
        cost: float = 1.0,
        kind: str = "normal",
        pre_command: str | None = None,
        tags: str | tuple[str, ...] = "",
    ) -> Exit:
        self.get_room(source_room_id)
        self.get_room(destination_room_id)
        direction = self._clean_text(
            direction, field="direction", max_len=MAX_EXIT_COMMAND, required=True
        )
        command = self._clean_text(
            command or direction, field="command", max_len=MAX_EXIT_COMMAND, required=True
        )
        cost = float(cost)
        if not (cost > 0):
            raise ValueError("exit cost must be > 0")
        kind = self._clean_exit_kind(kind)
        pre_command = self._clean_optional_command(pre_command)
        tags = self._clean_tags(tags)
        try:
            with self._conn:
                cur = self._conn.execute(
                    "INSERT INTO exits(source_room_id,destination_room_id,direction,command,cost,kind,pre_command,tags) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        int(source_room_id),
                        int(destination_room_id),
                        direction,
                        command,
                        cost,
                        kind,
                        pre_command,
                        tags,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("that source room already has an exit with this direction") from exc
        return self.get_exit(int(cur.lastrowid))

    def update_exit(self, exit: Exit) -> Exit:
        self.get_room(exit.source_room_id)
        self.get_room(exit.destination_room_id)
        direction = self._clean_text(
            exit.direction, field="direction", max_len=MAX_EXIT_COMMAND, required=True
        )
        command = self._clean_text(
            exit.command, field="command", max_len=MAX_EXIT_COMMAND, required=True
        )
        cost = float(exit.cost)
        if not (cost > 0):
            raise ValueError("exit cost must be > 0")
        kind = self._clean_exit_kind(exit.kind)
        pre_command = self._clean_optional_command(exit.pre_command)
        tags = self._clean_tags(exit.tags)
        try:
            with self._conn:
                cur = self._conn.execute(
                    "UPDATE exits SET source_room_id=?,destination_room_id=?,direction=?,command=?,cost=?,kind=?,pre_command=?,tags=? WHERE id=?",
                    (
                        exit.source_room_id,
                        exit.destination_room_id,
                        direction,
                        command,
                        cost,
                        kind,
                        pre_command,
                        tags,
                        exit.id,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("that source room already has an exit with this direction") from exc
        if cur.rowcount != 1:
            raise KeyError(exit.id)
        return self.get_exit(exit.id)

    def get_exit(self, exit_id: int) -> Exit:
        row = self._conn.execute("SELECT * FROM exits WHERE id=?", (int(exit_id),)).fetchone()
        if row is None:
            raise KeyError(exit_id)
        return Exit(**dict(row))

    def remove_exit(self, exit_id: int) -> bool:
        with self._conn:
            cur = self._conn.execute("DELETE FROM exits WHERE id=?", (int(exit_id),))
        return cur.rowcount == 1

    def exits_from(self, room_id: int) -> tuple[Exit, ...]:
        rows = self._conn.execute(
            "SELECT * FROM exits WHERE source_room_id=? ORDER BY direction,id",
            (int(room_id),),
        ).fetchall()
        return tuple(Exit(**dict(row)) for row in rows)

    def all_exits(self) -> tuple[Exit, ...]:
        rows = self._conn.execute(
            "SELECT * FROM exits ORDER BY source_room_id,direction,id"
        ).fetchall()
        return tuple(Exit(**dict(row)) for row in rows)

    def find_route(
        self,
        source_room_id: int,
        destination_room_id: int,
        preferences: RoutePreferences | None = None,
    ) -> tuple[RouteStep, ...]:
        source_room_id = int(source_room_id)
        destination_room_id = int(destination_room_id)
        self.get_room(source_room_id)
        self.get_room(destination_room_id)
        if source_room_id == destination_room_id:
            return ()
        prefs = self.route_preferences if preferences is None else preferences
        avoid_areas = {area.casefold() for area in prefs.avoid_areas}
        avoid_tags = {tag.casefold() for tag in prefs.avoid_tags}
        prefer_tags = {tag.casefold() for tag in prefs.prefer_tags}
        prefer_multiplier = float(prefs.prefer_multiplier)
        if not (0 < prefer_multiplier <= 1):
            raise ValueError("preferred-route multiplier must be > 0 and <= 1")

        queue: list[tuple[float, int]] = [(0.0, source_room_id)]
        best = {source_room_id: 0.0}
        previous: dict[int, tuple[int, Exit]] = {}
        while queue:
            cost, room_id = heapq.heappop(queue)
            if cost != best.get(room_id):
                continue
            if room_id == destination_room_id:
                break
            for edge in self.exits_from(room_id):
                tags = {tag.casefold() for tag in edge.tags.split() if tag}
                if avoid_tags & tags:
                    continue
                destination = self.get_room(edge.destination_room_id)
                if (
                    edge.destination_room_id != destination_room_id
                    and destination.area.casefold() in avoid_areas
                ):
                    continue
                edge_cost = edge.cost
                if prefer_tags & tags:
                    edge_cost *= prefer_multiplier
                new_cost = cost + edge_cost
                if new_cost < best.get(edge.destination_room_id, float("inf")):
                    best[edge.destination_room_id] = new_cost
                    previous[edge.destination_room_id] = (room_id, edge)
                    heapq.heappush(queue, (new_cost, edge.destination_room_id))
        if destination_room_id not in previous:
            raise ValueError("no route exists between those rooms under current route preferences")
        steps: list[RouteStep] = []
        current = destination_room_id
        while current != source_room_id:
            prior, edge = previous[current]
            steps.append(
                RouteStep(
                    edge.id,
                    prior,
                    current,
                    edge.direction,
                    edge.command,
                    edge.cost,
                    edge.kind,
                    edge.pre_command,
                    edge.tags,
                )
            )
            current = prior
        steps.reverse()
        return tuple(steps)

