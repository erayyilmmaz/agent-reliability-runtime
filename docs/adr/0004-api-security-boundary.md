# ADR-0004: Credential identity, tenant ownership and atomic Redis rate limiting

**Status:** Accepted; revised by ARR-20 / SEC-E1.

## Context

The original static SHA-256 key proved possession but did not bind a caller to
a tenant. Passing `X-Client-Id` to ownership filters let one valid credential
select another tenant. Caller-controlled rate-limit keys and separate Redis
increment/expiry operations also allowed bypass and immortal counters.

## Decision

Use an operator-managed credential registry with generated 256-bit secrets,
salted and peppered PBKDF2 verifiers, stable principal IDs, and tenant bindings.
All run/evaluation/replay ownership and idempotency scopes come from the verified
tenant. The existing database `client_id` column represents that tenant.

Redis uses one Lua operation for increment, expiry/orphan repair and TTL. The
budget follows the principal across credentials and API replicas. Redis failure
returns `503`; rejected requests do not enter API handlers. New append-only
security audits contain the verified tenant and a short registry-record
fingerprint, never a raw key or its verifier.

Default API-key mode refuses startup without valid secrets. Anonymous mode is
allowed only with explicit local environment configuration. Public access uses
an exact method/path allowlist containing only `GET /healthz`; API docs are
not registered in key mode. Compatibility client headers are validated but
cannot select authenticated ownership.

## Consequences

- Operator-managed rotation supports overlapping keys, revocation and expiry.
  Registry updates and revocations require every replica to restart.
- Existing legacy ownership must be verified before tenant bindings are issued.
  There is no automatic trust assignment or rewrite of historical audit rows.
- Local Compose explicitly opts into anonymous development. Production Helm
  defaults to authenticated operation and external secret provisioning.
- Principal rate limiting does not replace ingress controls for unauthenticated
  request floods or the KDF verification cost.
- Detailed provisioning and migration are in
  [the SEC-E1 operations guide](../security/credentials.md).
