# Mapper 5 — Visual Map & Navigation Usability

Mapper 5 adds a graphical projection of the existing profile-scoped mapper
without changing the mapper's transport or movement authority.

## Added

- `mapper_layout.py`: UI-neutral deterministic room positioning.
- `qt/map_view.py`: a read-only `QGraphicsView` map renderer.
- Room nodes and directed-exit lines with arrowheads and exit tooltips.
- Current-room highlighting.
- Active-route and route-preview highlighting.
- Distinct line styles for normal, door, and special exits.
- Click-to-select synchronization between the graphical map and room table.
- Mouse-wheel zoom and drag-to-pan navigation.
- **Fit Map** and **Center Current** controls.
- Manual room coordinate editing from the Mapper dock.
- Coordinate-assisted layout: explicit X/Y coordinates remain authoritative;
  rooms without coordinates receive deterministic fallback positions.
- A read-only active-route snapshot on `MapperWalker` for status/visualization.

## Architecture boundary

The visual widget has no `MudSessionController`, `MudConnection`, socket, or
command-pipeline reference. Its only outbound callback is `room_id selected`.
All map mutation remains repository/controller-owned, and all movement remains
controller/walker-owned through `CommandSource.MAPPER`.

## Layout policy

Coordinates are interpreted as mapper grid coordinates. Qt's downward Y axis
is inverted so positive mapper Y appears upward. Z/floor values receive a small
diagonal offset when rooms otherwise overlap. Missing coordinates are placed on
a stable fallback grid to the right of trusted coordinate data.

Mapper 5 intentionally does not persist graphical camera position or fabricate
room coordinates. The fallback layout is a presentation detail, not map truth.

## Deferred

- MUD-specific adapters.
- Persisted per-profile adapter selection.
- User-dragged room-position persistence.
- Terrain/area styling.
- Route avoidance/preferences.
- Minimap/full-map split views.

## Validation

The release includes UI-independent layout tests, structure/invariant tests for
the no-transport visual boundary, walker route-snapshot coverage, and a real
PySide6 graphical rendering test that runs automatically when PySide6 is
available. Final package validation: **230 passed / 3 skipped** in the current
build environment; the three skips are PySide6 runtime tests because PySide6 is
not installed here.
