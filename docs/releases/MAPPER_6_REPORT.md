# Mapper 6 Report — Route Preferences & Map Organization

## Scope

Mapper 6 adds profile-scoped route policy and map-organization tools without changing the transport or adapter boundaries.

## Routing policy

Exit records now support normalized route tags. The mapper database also stores a route policy containing avoided areas, avoided tags, preferred tags, and a preferred-tag cost multiplier.

- Avoided exit tags are hard exclusions.
- Avoided areas are not used as intermediate rooms. The source and requested destination remain legal even if their area appears in the avoid list.
- Preferred tags reduce effective Dijkstra cost only for route selection. They do not mutate the edge's stored base cost.
- Initial walking and controlled rerouting both use the same persisted policy because `MapperRepository.find_route()` owns the default policy lookup.

## Map organization

The Mapper dock now has an area-focused view filter. It filters both the room table and visual graph without deleting or changing hidden map data.

Room nodes can be dragged in the graphical map. Their manual visual position is persisted separately as `layout_x/layout_y`; protocol/world `x/y/z` coordinates remain untouched. A selected room's manual layout can be cleared to return it to coordinate-driven or deterministic fallback placement. Camera pan/zoom remains session-local in this release.

## Persistence and migration

Mapper schema version is now 4. Existing schema v1-v3 databases migrate in place. New columns are nullable/manual-safe and preserve historical map data. Route policy is stored in `mapper_meta`, so it naturally follows the existing profile-scoped SQLite database.

## Boundaries

The graphical map still has no controller, transport, or command-pipeline authority. Dragging a room writes only mapper layout state through the dock/controller-owned repository. Movement continues through the mapper walker and `CommandSource.MAPPER`.

## Deferred

- MUD-specific Discworld/Aurora adapters remain deferred until protocol samples exist.
- Camera persistence remains deferred until real usage justifies a profile contract.
- Area rename/merge and bulk room organization are candidates for Mapper 7.
