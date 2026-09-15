# Mapper Foundation 1

This milestone introduces a generic, SQLite-backed mapper foundation without assuming any MUD-specific room protocol.

## Included

- UI-neutral `MapperRepository` using SQLite with foreign keys enabled.
- Rooms with area, optional x/y/z coordinates, and notes.
- Directed exits with direction label, command, and positive traversal cost.
- Deterministic weighted shortest-path routing (Dijkstra).
- Profile-scoped map databases under `profiles_data/`; unnamed sessions use `mapper.sqlite3`.
- Dockable Qt Mapper editor for adding rooms/exits, previewing routes, and sending one selected exit.
- Mapper movement goes through `CommandSource.MAPPER` and never accesses transport directly.

## Deliberately deferred

- Automatic room discovery from GMCP/MSDP/text.
- MUD-specific adapters.
- Automatic/speed walking.
- Graphical map rendering.
- Special-exit scripts or conditional exits.

Automatic walking is deferred until the client has an explicit movement-completion contract. Foundation 1 never guesses that arbitrary output means a movement step completed.
