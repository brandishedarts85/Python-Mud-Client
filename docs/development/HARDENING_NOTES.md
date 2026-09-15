# Hardening notes

This build intentionally adds no new user-facing features. It addresses two
release-blocking foundation defects and adds regression coverage for them.

## MCCP2 decompression bound

`client_core.py` now defines:

```python
MAX_MCCP_OUTPUT_PER_READ = 1024 * 1024
```

MCCP2 data is decompressed with `decompress(..., max_length)` rather than an
unbounded `decompress(data)` call. The call asks zlib for at most the configured
budget plus one sentinel byte. If that sentinel byte can be produced, the
connection is rejected with `TelnetProtocolError` before the decompressed output
is passed to the Telnet/text parser.

The important distinction is that the bound is enforced *inside zlib*; this is
not a post-hoc `len()` check after an unlimited expansion has already happened.

The limit is per socket read because a MUD connection is an intentionally
long-lived compressed stream; imposing a finite lifetime decompressed-byte cap
would incorrectly terminate normal sessions. Network reads are 4096 bytes, so
this bounds one event-loop read cycle while still permitting long normal
sessions.

Regression coverage includes a highly compressible zlib payload whose expansion
is one byte over the permitted output budget.

## Malformed persisted automation

`MudSessionController` no longer calls `load_automation()` in a way that lets a
bad `automation.json` abort construction.

Automation loading is now transactional at the session boundary:

- a fresh automation engine is created;
- the persisted file is fully validated and loaded into that engine;
- on `ValueError`/read failure, the fresh empty engine is retained;
- the source file is left untouched;
- the error is recorded on `automation_load_error`;
- once the UI bridge is subscribed, `start()` emits a visible system warning.

This applies both to the global automation file and profile-scoped automation
files.

Regression coverage includes malformed JSON and a syntactically valid document
containing an invalid persisted trigger regex. The latter also verifies that a
valid alias earlier in the same file is not partially applied.

## Test correction

The existing trigger-replacement test previously swallowed the expected regex
exception with a generic `try/except`. It now uses `pytest.raises(...)` and then
asserts that the previously valid trigger remains intact.

## Hardening 2: MCCP2 orderly stream-end and trailing-data verification

The MCCP2 implementation already handled `zlib.decompressobj().eof` by ending
compression and placing `unused_data` back into the raw Telnet queue. This pass
adds explicit regression coverage around that boundary rather than changing the
transport logic without evidence.

Verified cases:

- clean zlib `Z_FINISH`/EOF delivers decompressed text exactly once;
- MCCP state is reset (`_mccp_active == False`, `_decompressor is None`);
- bytes in `unused_data` are reparsed as ordinary Telnet in the correct order;
- a Telnet negotiation immediately after the compressed stream is handled as
  Telnet, not fed to zlib;
- an EOR-delimited prompt immediately after the compressed stream survives the
  transition and is glued correctly by `MudSessionController`;
- raw bytes arriving in a later socket read remain raw after MCCP EOF;
- malformed zlib input raises `TelnetProtocolError` and is never reinterpreted
  as game text;
- the activation marker, compressed payload, orderly stream end, and following
  raw data can all occur in one socket read without losing the transition.

A TCP connection ending while an MCCP stream is still active is not treated as
an orderly MCCP shutdown merely because the socket ended. The protocol's
orderly transition is the zlib stream end (`Z_FINISH`), detected through
`decompressor.eof`.

## Hardening 3

This pass keeps feature work frozen and focuses on failure containment and
bounded retained state.

### Fail-soft connection profiles

`load_profiles()` remains the strict, whole-file validator.  A new
`load_profiles_safely()` application wrapper catches persisted-data/read
failures, returns an empty profile set plus an error string, and never rewrites
the bad file.  Saved-profile command-line startup and the Qt Profiles dialog
use this wrapper so a malformed `profiles.json` no longer aborts the client or
the profile UI.  Save/delete failures are reported instead of escaping through
Qt callbacks.

### Automation startup helper cleanup

`MudSessionController._load_automation_safely()` is now a static loader that
returns an immutable `AutomationLoadResult`.  It loads into a fresh engine and
preserves the persistence layer's atomic whole-file validation.  The controller
applies that result separately and reports failures after listeners are
subscribed.

### MSDP recursion bound

Remote MSDP nesting is capped by `MAX_MSDP_NESTING = 64`.  Pathologically deep
arrays/tables are dropped by the MSDP handler instead of risking Python
recursion exhaustion.  Tests cover both the accepted boundary and rejection of
excess nesting.

### Bounded protocol snapshots

The retained GMCP/MSDP inspector snapshots now cap distinct keys at 512 per
session.  Existing keys update normally; a new key at capacity evicts the
oldest retained key.  Event delivery itself is unchanged.

### Bounded ANSI logical lines

Transport chunks were already bounded, but a server could previously send an
unterminated logical line forever and grow `AnsiParser._current` without a
ceiling.  `MAX_LOGICAL_LINE_CHARS` now caps a logical ANSI line at 1 MiB.  On
violation the session resets presentation parsing, reports the discarded input,
and keeps the transport alive.  A regression test verifies recovery on the next
normal line.

### Bounded command history

Per-session command history is capped at 2,000 entries.  The newest entries are
retained and history navigation semantics remain unchanged.

### Transactional macro UI saves

Numpad macro edits and the master enable toggle now roll back their in-memory
state if persistence fails, rather than displaying unsaved state as though it
were durable.

### Verification

Hardening 3 adds adversarial tests for malformed profiles, static fail-soft
automation loading, deep MSDP nesting, protocol-snapshot eviction, ANSI
unterminated-line growth, parser recovery, and bounded command history.

## Hardening 4

This pass keeps feature work frozen and closes two remaining structural input/
persistence gaps.

### Atomic persistence durability

All JSON-backed user state already wrote through a same-directory temporary file
and `os.replace()`.  Hardening 4 strengthens that path by explicitly syncing the
temporary file before replacement and, on POSIX filesystems that support it,
best-effort syncing the containing directory after the rename.  This protects
both the file contents and the directory entry across a sudden process/system
failure as far as the host filesystem API permits.

Regression coverage now verifies that:

- a simulated `os.replace()` failure leaves the previous JSON file byte-for-byte
  intact;
- failed atomic writes remove their temporary file;
- the temporary file is `fsync()`'d before the replacement occurs.

The strict whole-document validation behavior is unchanged.

### Telnet subnegotiation bounds

The generic Telnet `IAC SB ... IAC SE` payload ceiling remains 1 MiB for unknown
options, but known protocols are now bounded earlier while bytes are still being
collected:

- TTYPE: 4 KiB;
- MCCP2 activation subnegotiation: 4 KiB;
- GMCP: 256 KiB;
- MSDP: 256 KiB;
- unknown options: 1 MiB generic fallback.

This matters because checking GMCP/MSDP size only after `IAC SE` still permits the
subnegotiation buffer to retain the full generic allowance first.  The parser now
uses the option byte, which is known before the payload, to select the correct
ceiling on every appended payload byte (including escaped IAC bytes).

Oversized frames raise `TelnetProtocolError` and the normal read-loop failure
containment closes that transport rather than continuing with ambiguous Telnet
framing.

Regression coverage includes over-limit GMCP, MSDP, TTYPE, and unknown-option
subnegotiations.

### Verification

Hardening 4 raises the regression suite to 78 passing tests and the complete
Python tree compiles successfully.

## Hardening 5

This pass continues the parser/lifecycle audit without adding user-facing
features.

### Incremental text-decoder correctness

`_decode_text_prefix()` now uses Python's incremental codec state rather than
inferring an incomplete trailing codepoint from one `UnicodeDecodeError`.
This closes an edge case where a complete malformed byte appearing before a
valid multibyte character split across socket reads could cause the split
character to be replaced and consumed prematurely.

The invariant is now:

- malformed *complete* byte sequences are consumed with replacement so the
  receive buffer always makes forward progress;
- a valid trailing incomplete codepoint remains buffered for the next read;
- the two conditions can coexist in the same input chunk.

Regression tests cover clean UTF-8 splits, malformed bytes before a split
character, malformed complete sequences, invalid continuation recovery, and
the same behavior through `MudConnection` TEXT events rather than only the
helper function.

### Oversized ANSI control-string containment

The existing CSI/OSC/DCS-family length ceilings prevented indefinite parser
buffering, but an over-limit control sequence could previously abandon only
its leading ESC.  The remainder could then be reconsidered as visible MUD text,
which is undesirable for both presentation and trigger safety.

The ANSI parser now enters a small discard state for oversized controls:

- oversized CSI is discarded until its final byte;
- oversized OSC is discarded until BEL or ST;
- oversized DCS/SOS/PM/APC is discarded until ST;
- ST split exactly across two `feed()` calls is handled;
- `reset()` clears discard state;
- ordinary text following the real terminator resumes normally.

The discarded control body is never appended to the logical line or exposed to
style-aware/text triggers.

### Deep GMCP JSON containment

GMCP payload bytes were already bounded.  A syntactically valid but extremely
deep JSON value can still make Python's JSON decoder raise `RecursionError`.
That condition is now contained at the GMCP message handler just like malformed
JSON: the bounded raw value is preserved as text instead of allowing one remote
message to unwind the connection read loop.

### Shutdown generation invalidation

`MudSessionController.close()` now advances the transport generation before
retiring its connection.  Late queued EventBus callbacks from the old transport
can therefore no longer overwrite the final `closed` state or report stale
errors after teardown.  Regression coverage also verifies repeated `close()` is
idempotent and leaves no controller-owned tick/reconnect tasks.

### Configuration format versioning

No on-disk schema marker is introduced in this pass.  The current profile file
is intentionally a direct name-to-profile mapping, while automation/macros use
different top-level shapes.  Adding a marker to only some files would create a
partial migration contract.  Versioning is therefore deferred until a single,
backward-compatible migration layer can be introduced for all persisted user
state together.  Strict whole-file validation remains unchanged.

### Verification

Hardening 5 raises the regression suite to 91 passing tests.  The full Python
source tree compiles successfully.

## Full-system stick audit (post Hardening 5)

A feature-frozen cross-version audit found and fixed: automatic reconnect
self-cancellation, cancellation-unsafe transport teardown, deep-JSON fail-soft
escape, compact `#trigger` regex errors, an MSDP snapshot regression, loss of
the richer protocol inspector/status UI, tab-close teardown ordering, session
listener isolation, and command-history draft restoration.

See `../audits/FULL_AUDIT_REPORT.md` for severity classification, regression coverage,
and remaining known limitations.  The suite is 100/100 passing after this
pass.


## Victory Lap — Second Base: Telnet negotiation churn

The mature-client comparison exposed a denial/amplification class that payload-size limits do not cover: a peer can repeatedly send WILL/WONT/DO/DONT without ever constructing a large frame. The transport now tracks negotiation frequency per option in a short rolling window. Once a generous budget is exceeded, only negotiation for that option is ignored for a cooling period; normal text and other Telnet options remain active.

Accepted duplicate WILL/DO messages are also state-idempotent: an already-enabled option is not acknowledged repeatedly. This follows Telnet's state-negotiation intent and avoids sustaining a peer's WILL/DO loop. A suppression start emits one `PROTOCOL_NOTICE`, which the controller records in the bounded Protocol Events feed without overwriting actual negotiated state. Reconnect/reset clears all churn accounting.
