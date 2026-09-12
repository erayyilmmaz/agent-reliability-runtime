# SEC-E3 — Auditability, telemetry and data protection (ARR-24)

## Scope and rollout status

Implemented runtime controls: SEC-007, SEC-013, SEC-014, SEC-AUD-01, SEC-AUD-02,
and SEC-022 deterministic input minimization. The explicitly agreed SEC-022 scope
also includes a tested envelope-encryption/retention **prototype** and production
design. No production key-management provider has been selected. Neither the
prototype nor this document means existing database payloads are encrypted.

Apply migrations through `20260912_09` before deploying new processes; `_08`
adds audit target fields, `_09` adds the mutable `tenant_keys` registry. Neither
migration changes existing payloads, removes history, or destroys keys. Retention
has no scheduled job; the operator command defaults to dry-run. Back up and test
migrations in staging first. Hosted CI and a deployed security assessment are
separate from local evidence.

## Audit failure policy — SEC-007

`APP_AUDIT_FAILURE_POLICY=fail_closed` is the default. An authenticated request
does not reach its handler if the authentication audit cannot be persisted.
Sensitive reads additionally withhold their response if read-audit persistence
fails. These cases return `503 AUDIT_UNAVAILABLE`. Requests already denied for
auth/rate limiting stay denied; an audit outage never turns a denial into access.
`fail_open` is an explicit availability tradeoff, not the default: the decision
continues, but the failure still emits a metric and sanitized error event.

`APP_AUDIT_WRITE_TIMEOUT_SECONDS` defaults to 2. Sink failures/timeouts increment
`arr.security.events{category="audit_write",outcome="error"}`. They trigger the
critical ARRAuditWriteFailure rule. The SQL sink bounds strings at the final
persistence boundary: event/reason/fingerprint 64, outcome 16, tenant/principal
128; control and surrogate characters are replaced. Only technical identifiers
and server-selected reasons belong in these fields—truncation is not redaction.
The append-only audit trigger is unchanged.

## Sensitive read trail — SEC-AUD-01

Authenticated GETs for run, attempt, event, evaluation and job resources record
verified principal, tenant, target UUID, resource kind and ALLOWED/DENIED/ERROR.
Cross-tenant requests stay `404` but leave a denied trace. A target UUID has no FK,
so a missing target can be recorded without creating a run. Payloads, query
strings, raw URLs, keys and cursor contents are not audit columns. Authentication
failures are boundary audit events with no fabricated principal. Invalid UUID
paths are not represented as valid target IDs. Audit ALLOWED means the server
prepared/authorized the response, not proof a remote client received every byte.

## Exception diagnostics — SEC-013

JSON logging never renders arbitrary message arguments, raw `str(exc)`, traceback
source lines, locals or exception chains. It emits a fixed message and technical
context. Exception types are normalized to a finite set, with at most eight
`agent_runtime` module/line locations. Operators correlate run/attempt/error IDs,
trace IDs and code locations rather than reading provider error bodies.

All application span context managers disable OTel's automatic raw exception and
status-description recording. A safe exception event marks ERROR and supplies
only normalized type and code locations. Worker-handled provider failures also
record that event. The rule covers API, repository, provider, worker and outbox
span boundaries. It does not authorize additional third-party auto-instrumentation
with SQL/body capture. Review the [OTel exception sensitivity guidance](https://opentelemetry.io/docs/specs/semconv/exceptions/exceptions-logs/)
before adding instrumentation.

## Inbound trace trust — SEC-014

`APP_TRUST_INBOUND_TRACE_CONTEXT=false` drops external parent/state headers and
starts an independent server trace. Enable only behind a trusted gateway which
strips arbitrary public trace headers. This setting never grants authorization.
Duplicate trace headers are dropped. Explicit W3C propagation avoids baggage or
environment-selected propagators importing extra data.

The conservative supported parent format is version `00`, lower-case 32/16-digit
nonzero trace/span IDs and flags `00`–`03` (including current SDK random-ID flags).
Unsupported versions/flags are discarded. Tracestate is limited to 512 ASCII
characters, 32 unique members, bounded valid keys/values. Invalid state is dropped
while a valid parent may remain. State is opaque trusted-vendor data; do not put
PII there. The same sanitizer protects injection, broker publication and worker
extraction, including legacy persisted carriers. See [W3C Trace Context](https://www.w3.org/TR/trace-context/)
and [Trace Context Level 2](https://www.w3.org/TR/trace-context-2/).

## Metrics and alerts — SEC-AUD-02

`arr.security.events` has only category/outcome labels, never principal, tenant,
run, credential, error messages or request paths. Categories cover auth,
rate-limit, audit-write, quota, sensitive-read and dropped trace context.
`arr.provider.calls` records reserved provider invocations; existing retry metrics
track retry scheduling. Provider labels are normalized. A reservation precedes
the external call, so a crash at that boundary can conservatively over-count it.

Prometheus rules in `docker/security-alerts.yml`:

| Alert | Threshold | Hold |
| --- | --- | --- |
| ARRAuditWriteFailure | Any audit error increase / 5m | Immediate |
| ARRAuthenticationDeniedSpike | >5 denied auth / second over 5m | 2m |
| ARRRateLimitSpike | >0.1 denials / second over 5m | 2m |
| ARRQuotaExhaustion | Any quota denial increase / 5m | 1m |
| ARRRetryStorm | >1 retry / second over 5m | 2m |
| ARRProviderCallSpike | >10 provider calls / second over 5m | 2m |
| ARRTelemetryUnavailable | Collector scrape down | 2m |

These are starter operational thresholds, not per-tenant anomaly models. The
six security/rate rules are also provisioned under Grafana's ARR Security folder.
Prometheus `promtool test rules docker/security-alerts.test.yml` tests both firing
and quiet cases; CI runs it in the Prometheus image. Compose mounts the rules.
Kubernetes operators must install equivalent rules in their own monitoring stack.
No external notification recipient is configured by this change; choose and test
an approved contact point/Alertmanager route before relying on paging. Collector
reachability does not prove every application's exporter is healthy.

## Data minimization and encryption prototype — SEC-022

The deterministic provider returns only `{"accepted": true}` unless the operator
sets `APP_DETERMINISTIC_ECHO_INPUT=true`. Caller policy cannot enable echo. Even
opt-in mode never copies `policy_snapshot` into the result. The sample regression
dataset is version 2 and checks acceptance, not copied prompt text.

Prototype API: `KeyWrapper.wrap/unwrap`, `LocalKeyWrapper`, `TenantEnvelope`.
The local wrapper requires a random 32-byte key and uses AES-GCM. It is not a
production KMS or a password-derived encryption mechanism. A per-tenant random
256-bit DEK is stored wrapped in `tenant_keys`; the wrapped material includes a
tenant binding. AES-GCM field encryption uses random 96-bit nonces and associated
data containing version, tenant, run UUID and field. Swapping tenant/run/field or
tampering with ciphertext fails authentication. DEKs are not cached by the adapter.

Planned runtime coverage: `runs.input_payload`, `runs.result_payload` and caller
`policy_snapshot.instructions`. `protect_policy()` leaves operational policy
readable and isolates encrypted instructions. Evaluation results can contain
caller-selected paths, so the adapter also permits an `evaluation_result` field.
The pending runtime binding must cover evaluation copies, replay copies, hashing
and migration/backfill—not just initial submission. Do not manually encrypt active
runtime rows with the prototype: current workers do not decrypt those envelopes.

### Retention/erasure semantics

DEK granularity is the erasure unit: **whole tenant**, not single run. Retention
preview uses `APP_PAYLOAD_RETENTION_DAYS` (30 default), and excludes tenants with
fresh encrypted writes, fresh completed runs, or any unfinished run. This avoids
destroying fresh records just because one old run expired. A busy tenant can remain
ineligible indefinitely; time-bucket/per-run DEKs require a different key schema.

`agent-runtime-retention` is dry-run by default, reports at most 100 candidates,
and never deletes run/history rows. `--apply` requires matching `--tenant-id` and
`--confirm-tenant`; it rechecks eligibility under the tenant lock, clears only
the live wrapped key and sets `destroyed_at`, writing an audit event in the same
transaction. The CLI is trusted operator tooling using DB authority, not an
HTTP endpoint; `--principal-id` is an operator assertion, not credential verification.
There is no automatic timer or purge on startup. Tombstones refuse re-creation;
decrypt then fails with KeyDestroyedError. Dry-run does not destroy anything.

### Production gate — intentionally outstanding

Deleting the live wrapped DEK is **not sufficient** crypto-shredding if a backup
contains a recoverable copy and the wrapping key still decrypts it. Copies already
unwrapped in memory, provider storage, exports, existing plaintext rows and replicas
also remain outside that deletion. No legal-compliance claim is made here.

Before enabling production encryption/erasure:

1. Choose a KMS/Vault provider and tenant-level revocation/deletion semantics which
   make old wrapped-key copies unusable; an ordinary shared KMS key alone does not
   provide independent tenant deletion. Define delayed deletion/rollback windows.
2. Bind the adapter to every runtime read/write path, enforce no plaintext fallback,
   reject admission for erased tenants, and test rolling upgrades and backfill.
3. Enable and verify infrastructure encryption for PostgreSQL/WAL/snapshots, disk
   volumes and backups. The current Kubernetes/Terraform baseline does not create
   an encrypted managed database; this cannot be certified from this repository.
4. Approve retention duration, backup expiry, legal holds and erasure scope. Revoke
   old key versions, drain worker key material, and handle replay/evaluation copies,
   provider retention and exported data before reporting tenant erasure complete.
5. Run restore-from-backup erasure tests using the chosen production key service.
   Local adapter tests prove only the live registry behavior, not backup destruction.

## Verification evidence (2026-09-12)

- 232 tests passed with disposable PostgreSQL/Redis enabled; 82.71% line coverage.
- Ruff, formatting, strict mypy, and Helm lint passed.
- Real Compose smoke passed for submission, durable evaluation, anonymous regression
  rejection and replay. The synthetic OTLP export exposed `arr_security_events_total`
  and `arr_provider_calls_total` at the actual collector Prometheus endpoint.
- Real Grafana provisioning API returned six `arr-sec-*` rules; Prometheus reported
  all seven rules loaded. `promtool` firing/quiet fixtures passed for all seven.
- In-memory span-export checks found no exception messages, prompt/key text or raw
  chains. API tests cover audit outage/timeout, fail-open override, read suppression,
  cross-tenant denial and inbound trace trust/duplicate-header behavior.
- SQL tests cover append-only audit, sink bounds, concurrent DEK creation, field/run
  binding, dry-run preservation, explicit live-key destruction, tombstones and
  exclusion of fresh/active tenants. CLI confirmation and default preview are tested.

Hosted CI, alert notification delivery, production deployment, KMS revocation,
runtime encryption binding and backup-erasure validation are not claimed by this evidence.
