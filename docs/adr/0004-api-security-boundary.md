# ADR-0004: Use static hashed API keys and shared Redis rate limiting at the API boundary

**Status:** Accepted

## Context

The runtime exposes durable execution submission and read APIs. `X-Client-Id`
is a scoping identifier, not an authentication factor. A V0 deployment needs a
small production boundary without placing raw credentials in runtime data,
logs, traces, or audit records.

## Decision

When `APP_AUTH_MODE=api_key`, the server receives only a SHA-256 digest in
`APP_AUTH_API_KEY_HASH`. Requests authenticate through `X-API-Key` or bearer
authorization; the received key is hashed and compared in constant time.

Redis applies a fixed-window limit to a SHA-256 hash of `X-Client-Id`. The
limit runs before API handlers, so a rejected request cannot create a run,
outbox event, or provider call. PostgreSQL stores append-only audit records
with controlled event metadata and an optional credential fingerprint, never
the raw key, header, request body, stack trace, or provider output.

## Consequences

- This is deliberately a static V0 boundary, not user identity, RBAC, or key
  rotation management.
- Redis unavailability fails closed for authenticated API traffic (`503`) so
  rate limiting cannot silently disappear.
- Production deployers must inject the digest through a secret manager. Local
  Compose remains explicitly unauthenticated for the credentials-free demo.
