# ADR-0003: Model runs, attempts, and events separately

**Status:** Accepted

**Date:** 2026-09-06

## Context

A single run may be delivered repeatedly, retried, moved to another provider,
or recovered after a worker crash. A single mutable row cannot truthfully
preserve this history or distinguish the requested unit of work from each
execution claim.

## Decision

Persist three concepts:

- `Run`: durable user-visible unit of work and its current summaries.
- `RunAttempt`: one worker execution with lease, provider, timestamps, and
  outcome.
- `RunEvent`: append-only ordered facts explaining lifecycle transitions.

Completed records are immutable. Replay creates a new Run referencing the
original through `replay_of_run_id`.

## Consequences

- Provider fallback and retry history are explicit and auditable.
- Current status reads may use denormalized Run fields, while events remain the
  diagnostic history.
- The model requires referential integrity, event ordering, and retention
  policy, which ARR-3 will implement.
- Provider-specific outcomes do not pollute the global run state machine.
