# SEC-E1 credential and tenant boundary (ARR-20)

## Configuration and identity

`APP_AUTH_MODE=api_key` and `APP_ENVIRONMENT=production` are the defaults.
Missing registry/pepper, malformed records, duplicate key IDs, a principal
mapped to multiple tenants, or a registry with no active key prevents startup.
Anonymous mode requires the explicit pair `APP_ENVIRONMENT=local` and
`APP_AUTH_MODE=disabled`. This pair is for trusted local development only.

The registry is a JSON array in `APP_AUTH_CREDENTIALS`. Each entry contains
`key_id`, `principal_id`, `tenant_id`, `scheme`, `salt`, `verifier`, `enabled`,
and optional timezone-aware `expires_at`. The separate `APP_AUTH_PEPPER` is
required and must contain at least 32 characters; generate it randomly.
Keep both in secret management, with the pepper separately controlled from
the credential registry. No raw API keys belong in the runtime registry.

Keys have the form `arr_<random key ID>.<256-bit random secret>`. Provisioning
uses `secrets.token_urlsafe(32)`; caller-selected passwords are not supported.
Verification uses domain-separated HMAC peppering and PBKDF2-HMAC-SHA256 with
600,000 iterations, a fresh 128-bit salt, and constant-time comparison. The
work factor follows the [OWASP PBKDF2 guidance](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html).
Verification runs in the bounded worker thread pool instead of blocking the
ASGI event loop. Apply ingress abuse controls for unauthenticated traffic;
principal rate limiting runs after authentication, not before KDF work.

The verified tenant supplies the existing `runs.client_id` ownership column
and its idempotency scope. All run/history/evaluation/replay operations use
this tenant. Multiple principals in one tenant intentionally share its runs.
Each principal has a separate shared Redis budget; all its rotated keys share
that budget. Principal IDs must remain stable across rotations and API replicas.

In authenticated mode, `X-Client-Id` is optional and cannot change identity.
For compatibility, if present it must fully match
`[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`. Empty, whitespace, duplicate, invalid, and
overlong headers return `422` before database or audit writes. In local mode,
this validated header still supplies the development tenant.

## Offline provisioning

The CLI does not open an API, database, or provider connection. Run it on a
trusted admin workstation. Its stdout intentionally contains a newly generated
raw key for secure delivery; do not send that output to shared logs or Git.
Example (requires `jq`, with output files outside the repository):

```bash
umask 077
export APP_AUTH_PEPPER="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
uv run agent-runtime-credential --principal service-a --tenant tenant-a \
  --expires-at 2027-01-01T00:00:00+00:00 > /private/tmp/arr-issued.json
export APP_AUTH_CREDENTIALS="$(jq -c '[.credential]' /private/tmp/arr-issued.json)"
export APP_ENVIRONMENT=production
export APP_AUTH_MODE=api_key
```

Use `.api_key` from the output for the caller's secret manager. Transfer the
registry and pepper to the runtime secret manager. Remove the provisioning
file after delivery using your approved secret-handling process. Never include
it in a support bundle. Start the API via `uv run agent-runtime-api`; direct
ASGI startup is `uvicorn agent_runtime.api.main:create_app --factory`.

## Rotation, expiry and revocation

1. Generate a new credential with the same principal/tenant and existing pepper.
2. Append its record to the registry; retain the old record during overlap.
3. Roll out the updated registry to every API replica. Both keys now work.
4. Switch the caller to the new key and verify its access and audit fingerprint.
5. Remove the old record or set `enabled=false`, then restart every API replica.
   Verify that the old key returns `403`, while the new key retains ownership
   and the existing rate-limit budget.

Configuration is a startup snapshot, not a hot-reloaded database registry.
Revocation is complete only when all replicas have restarted with the new
configuration. `expires_at` is checked on every authentication without restart.
Changing the pepper requires reissuing keys/verifiers and a coordinated rollout;
this version does not support simultaneous peppers. Do not reassign an existing
principal to another tenant or change a principal ID to rotate a key.

New audit entries contain a 16-character fingerprint derived from the public
registry key ID with an audit-specific prefix. It is not a hash of the raw key
and is not the credential verifier. Failed authentication records have no
asserted tenant or credential fingerprint. Existing append-only audit rows
are retained, including old SHA-256 fingerprints; rotate every legacy key.

## Upgrading the legacy static-key deployment

`APP_AUTH_API_KEY_HASH` cannot establish tenant ownership and is unsupported.
Provision fresh keys and verified server-side tenant bindings before rollout.
Existing `runs.client_id` values originated in untrusted headers. An administrator
must verify their ownership before binding a production tenant to those values.
Do not automatically grant a new credential access to a historical client ID
based on a caller's assertion. Unverified legacy data must remain inaccessible
until its ownership is resolved. No automatic data reassignment or destructive
schema migration is performed by this change.

Only the exact `GET /healthz` endpoint is public. API-key deployments do not
register `/docs`, `/redoc`, or `/openapi.json`; all future routes require auth
unless deliberately added to the method/path allowlist. Local docs stay enabled.

## Verification

`uv run pytest` covers malformed identities, startup refusal, identity binding,
rotation/revocation/expiry, every run-scoped endpoint, docs, future routes, and
rate-limit rejection (including the headerless regression API).

The CI quality job also supplies isolated PostgreSQL/Redis services and runs
`tests/e2e/test_security_live.py`. For local use, explicitly point
`ARR_TEST_DATABASE_URL` and `ARR_TEST_REDIS_URL` at disposable services, then run
`uv run pytest tests/e2e/test_security_live.py`. The test upgrades that database,
creates test runs/audits and verifies real SQL ownership and Redis concurrency,
TTL restoration, expiry, and rotated-key budgeting. It never drops a schema.
