# VS Code Agent Review — 2026-09-14

## Source of this note

The project owner reviewed Mapper 6 in VS Code using its agents and supplied the
resulting review summary with the corrected project ZIP. This file preserves that
review in the repository and records the independent verification performed during
the clean-repack pass.

## Reported functional fixes

The external review identified and fixed five real failure modes:

1. `client_core.py` — deeply nested GMCP JSON could overflow recursion or produce runaway fallback/log behavior.
2. `persistence.py` — pathological nested JSON files could overflow Python's decoder recursion.
3. `qt/map_view.py` — mapper graphics-scene initialization was incorrect.
4. `transcript.py` — timezone-aware timestamps were being converted unexpectedly.
5. `qt/docks/mapper.py` — source text contained content that caused Windows encoding trouble.

## Independent verification

The clean-repack audit confirmed the corresponding defensive code and regression
coverage are present. In the Linux verification environment the suite reports
`245 passed, 3 skipped`; the skipped PySide6 modules contain four tests total.
The owner's VS Code/PySide6 environment reported `249 passed`, which is consistent
with those four Qt tests executing instead of being skipped.

`python -m compileall -q .` also passes, and Python source files decode as UTF-8.

## Cleanup completed during repack

- Extracted the JSON nesting threshold into the named `MAX_JSON_NESTING` constant.
- Moved historical engineering documents into the `docs/` hierarchy.
- Removed `.venv`, `__pycache__`, `.pytest_cache`, `*.pyc`, and other generated material from the release artifact.
- Added `CHANGELOG.md` and this review record.

No new MUD/client behavior was intentionally introduced by the repack.
