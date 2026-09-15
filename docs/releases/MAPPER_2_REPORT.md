# Mapper 2 — Room Identity & Adapter Seam

Mapper 2 adds protocol-derived room identity without coupling the mapper database, transport, or Qt presentation to a specific MUD.

## Architecture

Protocol events flow through the session controller into a selected mapper adapter. Adapters return immutable `RoomObservation` values and have no controller, socket, repository, or Qt access. The controller alone applies accepted observations to the active profile-scoped SQLite repository.

```
GMCP / MSDP
    -> mapper adapter
    -> RoomObservation
    -> session controller
    -> MapperRepository
    -> Mapper dock notification
```

## Generic GMCP adapter

The built-in `Generic GMCP Room.Info` adapter understands conservative, common Room.Info fields: stable room id (`num`, `id`, `roomid`, `room_id`, or `vnum`), name/title, area/zone, x/y/z coordinates, and exits.

A stable identifier is required. Room names are never used as identity because many MUDs reuse titles.

If an exit includes a destination room identifier, the repository creates a placeholder for that identity and creates/updates the directed exit. Later observation of that destination upgrades the same room instead of duplicating it.

## SQLite schema v2

Rooms now include nullable `external_id` and an observation `source`. Existing schema-v1 mapper databases migrate in place; prior manually-created rooms remain valid with `external_id = NULL` and `source = manual`.

## Adapter selection

The Mapper dock exposes adapter selection. `Manual only` disables protocol discovery. `Generic GMCP Room.Info` enables the conservative generic observer. Selection is session-local in this milestone; persistent per-profile adapter policy is deliberately deferred until MUD-specific adapters exist.

## Current-room state

The controller tracks `current_room_id` after a successful observation and the Mapper dock marks the current room. Changing profile scope resets current-room state rather than carrying an identity across databases.

## Still deferred

- MUD-specific Discworld/Aurora/etc. adapters
- prompt-aware automatic walking
- text-derived room parsing
- graphical map rendering
- special/conditional exits
- persistent per-profile adapter selection

## Validation

- 196 tests passed; 2 Qt runtime tests skipped because PySide6 is unavailable in the build environment.
- mapper v1 -> v2 migration regression covered.
- room identity upsert and placeholder destination behavior covered.
- manual adapter disables protocol discovery.
- Qt mapper dock remains free of protocol parsing and direct transport access.
