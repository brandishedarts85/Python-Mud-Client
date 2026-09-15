# Mapper 6 — Clean Repack Report

## Purpose

This checkpoint repackages the externally reviewed Mapper 6 tree as a clean,
reviewable repository baseline. It intentionally avoids a Python package/module
relocation so repository housekeeping cannot accidentally change runtime import
behavior.

## Source baseline

The source is the Mapper 6 ZIP returned from the owner's VS Code agent review.
That review contained fixes for deep GMCP payloads, pathological persisted JSON,
Qt mapper scene initialization, timezone-aware transcript timestamps, and mapper
source encoding on Windows.

## Release-artifact cleanup

Removed from the packaged tree:

- `.venv/` / `venv/`
- `__pycache__/`
- `.pytest_cache/`
- `*.pyc` / `*.pyo`
- transient coverage/cache artifacts

The prior review ZIP contained a roughly 694 MB Windows virtual environment. The
clean source tree is approximately 1 MB before ZIP compression.

## Documentation hierarchy

Kept at repository root:

- `README.md`
- `ROADMAP.md`
- `CHANGELOG.md`
- `LICENSE`

Moved historical and engineering material under:

```text
docs/
├── README.md
├── architecture/
├── audits/
├── development/
├── releases/
└── reviews/
```

Tests and internal links were updated to follow the new locations.

## Small cleanup included

`persistence.py` now names the defensive JSON depth ceiling as
`MAX_JSON_NESTING` rather than embedding the numeric threshold at the call site.
No functional limit changed.

## Validation

After the hierarchy change:

```text
247 passed
3 skipped
64 Python files compile
0 UTF-8 Python decode failures
0 generated cache/bytecode artifacts in the packaged tree
```

The three skipped test modules require PySide6 in this verification environment.
The owner's PySide6-equipped review environment previously reported 249 passing
tests before the two repository-organization regression tests added by this repack.
