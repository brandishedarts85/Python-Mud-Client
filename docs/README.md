# Documentation Index

This directory keeps engineering documentation separate from executable source.
The repository root is reserved for the main entry documents (`README.md`,
`ROADMAP.md`, `CHANGELOG.md`, and `LICENSE`) plus code/configuration.

## Architecture

- [`architecture/ARCHITECTURE_INVARIANTS.md`](architecture/ARCHITECTURE_INVARIANTS.md) — layer direction, ownership, lifecycle, and safety contracts.
- [`architecture/PERSISTENCE_SCHEMA.md`](architecture/PERSISTENCE_SCHEMA.md) — versioned JSON persistence and migration rules.
- [`architecture/QT_MIGRATION.md`](architecture/QT_MIGRATION.md) — historical Textual-to-PySide6 migration notes.

## Development notes

- [`development/HARDENING_NOTES.md`](development/HARDENING_NOTES.md) — defensive limits and adversarial regression rationale.
- [`development/TESTING_STRATEGY.md`](development/TESTING_STRATEGY.md) — test taxonomy and release gates.

## Audits and references

- [`audits/FULL_AUDIT_REPORT.md`](audits/FULL_AUDIT_REPORT.md) — stick-audit findings and coverage.
- [`audits/REFERENCE_AUDIT.md`](audits/REFERENCE_AUDIT.md) — lessons from external/reference clients.

## Release reports

`releases/` contains milestone engineering reports. They are historical records,
not the current roadmap. For current status, use the root-level `ROADMAP.md`.

- `HOME_PLATE_REPORT.md`
- `SETTINGS_1_REPORT.md`
- `LOGGING_SEARCH_1_REPORT.md`
- `VARIABLES_1_REPORT.md`
- `MAPPER_FOUNDATION_1_REPORT.md`
- `MAPPER_2_REPORT.md` through `MAPPER_6_REPORT.md`
- `MAPPER_6_CLEAN_REPACK_REPORT.md`
- `MUD_ADAPTERS_1_REPORT.md`
- `VICTORY_LAP.md`

## Reviews

`reviews/` records external or independent review passes and their verification
status. Review notes do not replace executable tests or release reports.

- [`reviews/2026-09-14-vscode-agent-review.md`](reviews/2026-09-14-vscode-agent-review.md)

## Where new documents should go

- Architecture contracts/design boundaries -> `docs/architecture/`
- Testing/hardening/developer notes -> `docs/development/`
- Comparative/reference audits -> `docs/audits/`
- Milestone/release reports -> `docs/releases/`
- External review notes -> `docs/reviews/`
- Current plans/status -> keep `ROADMAP.md` at repository root
