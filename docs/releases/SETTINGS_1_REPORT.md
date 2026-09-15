# Settings & Appearance 1

This is the first post-victory-lap feature milestone. It intentionally adds a
small global settings surface without weakening the architecture or persistence
contracts established during hardening.

## Added

- UI-neutral immutable `ClientSettings` model with strict validation.
- `settings.json` persistence using the shared v1 document envelope.
- fail-soft startup loading; malformed/future-version files remain untouched.
- byte-exact `.pre-v1.bak` preservation if a legacy unversioned settings file is
  ever upgraded by a save.
- Qt Settings dialog with font chooser, font size, default text/background
  colors, timestamps, scrollback limit, local echo, and new-session reconnect
  defaults.
- reversible live preview for font/colors only.
- live application of committed settings to every open session.
- new unnamed sessions inherit global reconnect defaults; saved profiles keep
  their profile-specific reconnect settings.
- scrollback ring resizing preserves the newest retained lines.
- user-controlled local command echo still remains subordinate to Telnet
  `WILL ECHO`, so server-side password echo rules remain authoritative.

## Deliberately deferred

- customizable ANSI 16-color palette (the parser currently normalizes ANSI
  colors to RGB before presentation).
- UI density/dock preset themes.
- advanced logging/search preferences beyond the bounded defaults introduced in Logging & Search 1.

## Test environment note

The build environment used for this package does not have PySide6 installed.
The Qt runtime settings test therefore skips in this environment. Qt source
architecture checks still run, all Python source compiles, and the non-Qt
settings/persistence/controller tests execute normally. The project requirements
continue to specify PySide6 for a normal desktop installation.
