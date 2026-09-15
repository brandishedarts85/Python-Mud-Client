# MUD Adapter Framework 1 Report

## Goal

Turn the Mapper 2 adapter seam into an explicit extension contract without moving MUD-specific behavior into transport, session core, Qt, or SQLite.

## Added

- `MapperAdapterRegistry` with validated registration and duplicate-key protection.
- `MapperAdapterDescriptor` and `MapperAdapterCapability`.
- Capabilities currently cover stable room identity, GMCP, MSDP, exits, area, and coordinates.
- `register_mapper_adapter()` for optional modules that intentionally register with the default client registry.
- Registration-time validation that a factory returns an adapter with the declared key/label and required observation methods.
- Controller routing and auto-walk policy now inspect declared capabilities instead of checking adapter names such as `manual`.
- Protocol observation calls are skipped entirely when the selected adapter does not declare that protocol capability.

## Evidence-backed adapter

The first protocol-specific adapter is `MG.room.info` (`mg-room`). Its shape is based on the previously audited MudPyC reference implementation, whose LP-MUD/Morgengrauen-family drivers consume an `MG.room.info` payload with:

- `id` — stable room identity;
- `short` — room title;
- optional `area`;
- `exits` — direction to destination-room-id mapping.

The reference also contains a concrete payload sample with room id `5833`, title `Tower of Light`, area `Manetheren`, and numeric destination IDs. The client adapter implements only that evidenced subset.

## Deliberately not added

No adapter is labeled Discworld or Aurora in this release. We do not yet have a captured/documented room-protocol shape for either in this client project, and inventing one would violate the adapter boundary's purpose.

## Safety / architecture

Adapters receive protocol package/value data and may return immutable `RoomObservation` values. They receive no Qt widgets, session controller, `MudConnection`, `CommandPipeline`, or `MapperRepository` references.

Automatic walking still requires the selected adapter to declare stable room identity. Merely being registered, having a non-`manual` key, or consuming GMCP is not sufficient.
