# ADR-0001: Persist an outbox event transactionally with every accepted run

**Status:** Accepted

**Date:** 2026-09-06

## Context

Persisting a run and publishing a RabbitMQ message are separate durable
systems. Publishing before the database commit can produce a message for a run
that does not exist. Committing before publishing can leave an accepted run
with no delivery message if the process dies.

## Decision

`POST /runs` will write the Run and an OutboxEvent in one PostgreSQL
transaction. A separate dispatcher reads unpublished outbox events, publishes
them with broker publisher confirmation, and records publishing progress. The
dispatcher is independently retryable.

## Consequences

- `202 Accepted` means delivery intent is durable, not that RabbitMQ has
  accepted the message yet.
- The design tolerates dispatcher crashes and broker outages without losing
  accepted work.
- An outbox event can be published more than once; consumers must remain
  idempotent.
- The persistence and dispatcher work add operational tables and monitoring,
  but remove the unresolvable dual-write gap.
