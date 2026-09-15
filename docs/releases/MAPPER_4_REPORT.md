# Mapper 4 — Special Exits, Doors, and Controlled Rerouting

## Scope

Mapper 4 extends the acknowledgement-driven walker from Mapper 3 to handle two
common real-world navigation cases without relaxing its safety contract:

1. exits that require a preparatory command, such as opening a door; and
2. movement that lands in a different but positively identified mapped room.

It also adds the requested root-level `ROADMAP.md` with a concise current and
planned feature outline.

## Mapper schema v3

Exit records now carry:

- `kind`: `normal`, `door`, or `special`;
- `pre_command`: an optional command that must complete before movement.

Existing schema-v2 databases migrate in place. Historical exits become
`normal` exits with no pre-command.

Protocol room observations do not erase richer user-edited exit metadata. If a
user upgrades an observed `north` exit into a door with `open north`, later
`Room.Info` refreshes may update its destination identity but preserve the
custom command, cost, type, and pre-command.

## Door / pre-command sequencing

For a route step with a pre-command the walker now performs:

```text
send pre-command
      ↓
wait for prompt boundary
      ↓
send movement command
      ↓
wait for expected room identity + prompt boundary
      ↓
advance
```

The client never sends `open north` and `north` back-to-back without waiting for
one command cycle to finish.

If the door command fails, the subsequent movement may also fail; in that case
no destination room acknowledgement arrives and the movement step times out
rather than being assumed successful.

## Special exits

A special exit is a mapped edge whose movement command can be arbitrary, such
as `enter portal`, `climb rope`, or another MUD-specific command. It still obeys
exactly the same room-identity + prompt acknowledgement requirement as ordinary
movement.

## Controlled rerouting

Mapper 3 stopped immediately on any unexpected room. Mapper 4 can recover only
when all of the following are true:

- the unexpected location came from a stable room-identity observation;
- the controller can calculate a mapped route from that room to the original
  destination;
- every destination on the replacement route has stable external identity;
- the reroute budget has not been exhausted.

The default budget is three reroutes for one walk. A replacement route is not
started until the prompt boundary for the movement that caused the divergence
has arrived. This prevents a room update from triggering a command burst before
the previous command cycle has settled.

If no safe route exists, an unidentified room would be required, the route is
invalid, or the reroute budget is exhausted, walking stops.

## UI

The Mapper dock now supports:

- exit type selection (`normal`, `door`, `special`);
- optional pre-command;
- editable route cost;
- **Edit Exit…** for existing edges;
- route previews that show exit type and preparation commands.

Manual **Send Exit…** remains a single movement-command action. It does not run
pre-commands automatically because prompt-aware sequencing belongs to the
walker and requires stable room identity.

## Safety boundaries retained

- The Mapper dock has no direct transport access.
- All mapper commands use `CommandSource.MAPPER` and the shared command
  pipeline.
- The walker owns no sockets, Qt objects, asyncio tasks, or SQLite connection.
- Route recovery is controller-owned and repository-backed.
- Prompt-only success is never accepted as movement success.
- Room-only success is never enough to advance the next movement command.

## Deferred

Graphical map rendering, game-specific door-state parsing, persisted adapter
selection, route-avoidance tags, and richer MUD-specific adapters remain future
work. See `ROADMAP.md` for the current plan.
