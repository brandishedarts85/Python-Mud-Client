# Logging & Search 1

This milestone adds bounded transcript logging, per-session search, and durable
plain-text export without moving logging into the protocol/transport layer.

## Added

- UI-neutral `TranscriptLogger` with explicit start/stop ownership.
- UTF-8 log records with offset-aware ISO timestamps.
- 5 MiB per-segment rotation with three retained backups by default.
- log failure containment: disk/permission/rotation failures stop only logging;
  the triggering MUD line still renders and the session continues.
- per-session **Start Session Logging…** / **Stop Session Logging** actions.
- a per-session `Ctrl+F` find bar with next, previous, wrap-around, and
  case-sensitive matching.
- atomic **Export Transcript…** to plain UTF-8 text.
- search and export operate on the bounded rendered transcript rather than
  creating an unbounded duplicate history.
- transcript module remains Qt-independent and is covered by architecture
  checks.

## Logging semantics

Logging records what the user sees: visible MUD lines, local command echo, and
client system lines. Gagged trigger input is intentionally not logged because it
was not presented in the transcript. Raw Telnet/GMCP/MSDP traffic belongs in the
Protocol inspector rather than the user transcript.

The file logger always adds an ISO timestamp to each record. Rendered-output
timestamps remain a separate appearance preference. Plain-text export captures
exactly the currently rendered text, including visible timestamp prefixes when
that setting is enabled.

## Safety / boundedness

- active segment: 5 MiB default ceiling
- retained rotated segments: 3
- rotation never deliberately splits one transcript line
- logger failure cannot abort UI rendering or transport processing
- export uses write+flush+fsync+atomic replace
- search is bounded by the QTextDocument/scrollback retention setting

## Test environment note

The build environment does not contain PySide6, so the real Qt runtime search
and Settings dialog tests are skipped here. Static Qt integration/architecture
checks still execute, every Python source file is compiled, and the complete
non-Qt suite runs normally. On an environment with PySide6 installed, the Qt
search test exercises actual QTextDocument wrap and case-sensitive behavior.
