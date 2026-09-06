# ADR-0002: Keep provider execution outside the API process

**Status:** Accepted

**Date:** 2026-09-06

## Context

Provider and tool calls can be slow, rate-limited, retried, or interrupted.
Executing them in FastAPI request handlers couples client latency and API
availability to provider behaviour, makes crash recovery unclear, and exhausts
web-process capacity.

## Decision

The API validates and durably accepts work only. It creates a Run and Outbox
event, then responds with `202 Accepted`. A separate worker consumes queue
deliveries, obtains an execution lease, invokes providers, and records
attempts/events.

## Consequences

- API latency stays bounded by validation and durable persistence.
- Clients must poll or otherwise query a run for outcome rather than expecting
  a synchronous model response.
- Worker failures are recoverable via leases and queue redelivery.
- Worker and API can scale and deploy independently.
