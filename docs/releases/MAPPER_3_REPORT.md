# Mapper 3 — Prompt-Aware Walking

## Scope

Mapper 3 adds conservative automatic route walking on top of the Mapper 2
identity/adapter seam.  It intentionally does **not** fire an entire route at
once.

## Completion contract

Each route step requires both:

1. a room observation resolving to the mapped destination expected by that
   route step, and
2. a prompt boundary (Telnet GA/EOR/PROMPT or the existing idle-prompt
   fallback).

Only after both are present may the next movement command be sent.  Prompt-only
acknowledgement is insufficient because a failed movement can still produce a
prompt.  Room-only acknowledgement is insufficient because it can arrive before
the command cycle has settled.

## Safety behavior

- one command is in flight at a time;
- all movement uses `CommandSource.MAPPER`;
- every route destination must have stable external identity;
- manual-only adapter mode cannot auto-walk;
- unexpected room identity stops the route as divergence;
- a missing acknowledgement stops on timeout;
- disconnect, mapper-scope changes, adapter changes, session close, or explicit
  Stop Walk cancel the route;
- the Qt Mapper dock has no transport access.

## UI

The Mapper dock now exposes **Walk to Selected** and **Stop Walk** and displays
the current walker status.

## Deferred

MUD-specific movement failure messages, configurable timeout UI, doors/special
exits, rerouting, and higher-level process scripting remain deferred.  Those
features should extend the acknowledgement contract rather than bypass it.
