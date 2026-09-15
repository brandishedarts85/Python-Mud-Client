# Changelog

This file is a concise project history. Detailed engineering reports live under
`docs/releases/`.

## 2026-09-14 — Mapper 6 reviewed clean baseline

### Correctness fixes verified from the VS Code review

- Hardened deep GMCP JSON handling against recursion and runaway fallback/log behavior.
- Added pre-parse persistence JSON nesting protection.
- Corrected Qt mapper graphics-scene initialization.
- Preserved already-timezone-aware transcript timestamps.
- Removed mapper dock source-text encoding hazards observed on Windows.

### Repository/release cleanup

- Removed bundled virtual environments, bytecode, pytest caches, and generated material.
- Organized architecture, development notes, audits, reviews, and historical release reports under `docs/`.
- Added `docs/README.md` as the documentation index.
- Extracted the persistence JSON nesting threshold into `MAX_JSON_NESTING`.

### Verification

- External VS Code/PySide6 review: `249 passed`.
- Independent clean-repack environment: `245 passed, 3 skipped` (PySide6 runtime modules unavailable here; those modules contain four tests).

## Earlier milestones

See `docs/releases/` for Home Plate, Settings 1, Logging & Search 1, Variables 1, Mapper Foundation/2–6, MUD Adapters 1, and Victory Lap reports.
