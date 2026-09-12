# SEC-E2 — API resource controls (ARR-23)

This is an implementation contract, not a claim that the entire application has
passed an independent penetration test. Credential/tenant binding from SEC-E1
remains the authorization boundary. Limits apply to authenticated identities,
not caller-selected headers. Anonymous local mode is for trusted development only.

## Durable evaluation and regression jobs — SEC-002B / SEC-003

Evaluation submission validates bounded configuration and commits a PENDING
evaluation, a job run, and its outbox intent in one transaction. `202` proves
durable acceptance, not completion. Read `details.job_run_id` through
`GET /v1/jobs/{id}`, or page the source's evaluations. Workers evaluate persisted
successful results; evaluation PASSED/FAILED is independent of source execution
SUCCEEDED. Unexpected failure or lease exhaustion finalizes evaluation ERROR.

Regression submission requires API-key mode, verified principal, the ordinary
mandatory Redis rate limit, and `Idempotency-Key`. It accepts at most 10 cases,
and checks `2 * cases <= APP_REGRESSION_PROVIDER_CALL_BUDGET` before admission.
Workers recheck that budget and charge before every provider invocation. The job
receipt has `job_id` and `status_url`; its final `result` is the comparison report.
The synchronous CLI is a trusted operator tool and does not use SQL quotas.

Both job kinds reuse the normal outbox, manual-ACK worker, and lease recovery.
They have one attempt: uncertain paid execution is not retried automatically.
After investigating a failed job, explicitly resubmit; regression requires a new
idempotency key. Evaluations do not accept an idempotency key, so retrying their
POST creates a new quota-counted job. Replaying internal job runs is rejected.
Provider reservations are not an exactly-once provider execution guarantee.

## Safe rule subset — SEC-002

At most 16 rules are accepted. `matches`/regex execution is disabled. Generic
operators are `exists`, `equals`, `not_equals`, and `contains`; other rule types
are `non_empty`, `latency_budget`, and restricted `json_schema`.

Schema keywords: `type`, `properties`, `required`, boolean `additionalProperties`,
`items`, `enum`, `const`, `minimum`, `maximum`, `minLength`, `maxLength`, `minItems`,
`maxItems`. References, patterns, combinators, and `uniqueItems` are rejected.
Schemas are bounded to 128 nodes / depth 6, 32 properties per object, 32 enum
values. Rules and evaluated data have iterative depth/node/text limits before
validation. Unsupported or over-budget configuration returns `422`; result
evaluation runs off the API event loop, in the worker. Existing unsafe saved
rules must be rewritten; they are not grandfathered in.

## Policy and execution limits — SEC-004 / SEC-004B / SEC-020

`RunPolicyRequest` forbids unknown fields and non-finite numbers. Accepted fields
are `max_attempts`, `attempt_timeout_seconds`, `initial_backoff_seconds`,
`max_backoff_seconds`, `provider_order`, `routing`, `model`, `instructions`, and
`max_output_tokens`. Nested routing accepts only strategy and provider candidates.
Absolute input caps include 20 attempts, 600-second timeout, 8,192 output tokens,
two known providers, and 4,096 instruction characters. Invalid bounds/types
return `422`; valid requests are clamped to lower server-configured limits.

Defaults: 3 attempts, 60-second attempt timeout, 1-second initial backoff,
60-second maximum backoff, 2,048 output tokens. Retry delay has a hard one-second
floor even with legacy snapshots; exponent growth is bounded. Attempt
timeouts are kept shorter than the execution lease. Worker claims prevent
additional attempts beyond the current server cap. `APP_PROVIDER_TIMEOUT_SECONDS`
configures the OpenAI HTTP client's timeout; worker calls also have this outer
deadline. This is distinct from the overall attempt/job deadline.

## Streaming request boundary — SEC-005

`APP_MAX_REQUEST_BYTES` defaults to 131,072. The outer ASGI middleware counts
actual received bytes before buffering/parsing/authentication. Exceeding the
limit returns `413`, including chunked or length-less uploads. Duplicate,
malformed, conflicting Content-Length/Transfer-Encoding, or mismatched lengths
return `400`. A bounded read deadline (`APP_REQUEST_BODY_TIMEOUT_SECONDS`, 10 by
default) returns `408`. Application buffering is bounded; reverse proxies/ASGI
servers must also have connection, header, and transport-buffer limits.

## Shared quotas — SEC-RC-02

PostgreSQL is the common authority across API and worker replicas. Transaction
advisory locks use a consistent principal/tenant order; admission checks and
provider reservations are atomic. Backend failure cannot permit an uncharged
provider call. Every role must receive the same limits:

| Setting (`APP_` prefix) | Default | Meaning |
| --- | ---: | --- |
| PRINCIPAL_CONCURRENT_RUNS / TENANT_CONCURRENT_RUNS | 20 / 40 | Queued + running + retry-scheduled runs/jobs |
| PRINCIPAL_RUNNING_RUNS / TENANT_RUNNING_RUNS | 1 / 2 | Concurrent execution leases |
| PRINCIPAL_PROVIDER_CALLS / TENANT_PROVIDER_CALLS | 120 / 240 | Reserved calls per fixed window |
| QUOTA_WINDOW_SECONDS | 3600 | Database-clock fixed window |
| REGRESSION_MAX_CASES | 10 | Cases per regression job |
| REGRESSION_PROVIDER_CALL_BUDGET | 20 | Calls per regression job |

All run submissions, replays and jobs count toward active admission; idempotent
duplicates do not consume a new slot. Full admission returns `429` with
`RESOURCE_QUOTA_EXCEEDED`. A full execution pool persists a delayed retry without
creating an attempt; the scheduler requeues it no sooner than one second later.
This caps per-tenant occupancy; it is not a weighted-fair queue or a dedicated
capacity reservation for every tenant.

Each provider invocation, including deterministic calls, fallback attempts and
regression cases, reserves both principal and tenant quota before external work.
Quota exhaustion fails execution without another provider call. Reservations are
conservative: failures/crashes do not refund them. Fixed windows can permit a
burst at their boundary; they are not a rolling billing cap. Quotas cap calls,
not currency. Output-token caps provide an additional per-call bound.
Legacy rows without principal identity still count against tenant limits; new
rows always persist the verified principal (local mode uses a development identity).

## Bounded history — SEC-RC-01

`/attempts`, `/events`, and `/evaluations` keep their JSON list response but default
to 50 rows, maximum 100. Use `limit` and UUID `cursor`; forward ordering is
timestamp then ID. `X-Next-Cursor` is emitted for full pages (the last full page
can be followed by an empty page). A cursor must belong to this run and resource;
invalid cursors return `422`, inaccessible runs return `404`. Paging is live,
not a frozen snapshot. SDK `get_history_page()` returns `items` and `next_cursor`;
legacy `get_events()` / `get_attempts()` return the first page only.

## Upgrade and validation

1. Back up PostgreSQL and stop/drain old API, worker, dispatcher, and scheduler
   versions. Do not mix old workers with new job kinds or quota enforcement.
2. Apply `alembic upgrade head` (revision `20260912_07`) before starting new roles.
   The migration adds job identity, evaluation linkage, quota counters and indexes;
   it does not delete existing runs or history.
3. Configure identical limits for every role. Helm `config` values expose all
   controls; `.env.example` documents environment names. Raw Python processes
   require exported variables. Local Compose intentionally uses development defaults.
4. Update clients for asynchronous evaluation/regression and history pagination.
   Verify health, authenticated submission, job polling, and tenant isolation.

Evidence: `test_resource_controls.py` exercises hostile schema/policy/body inputs;
`test_resource_controls_live.py` exercises real PostgreSQL concurrency, Redis-backed
API access, cursor pagination, lease recovery, provider budgets and real HTTP
timeout changes. Compose smoke exercises durable evaluation through actual
RabbitMQ and rejects anonymous regression. Hosted CI/deployment are separate checks.

Local verification on 2026-09-12: 200 tests passed with the disposable PostgreSQL
and Redis services enabled, 81.61% line coverage; Ruff, formatting, mypy and Helm
lint passed. Helm rendering preserved non-default quota settings. Compose smoke
passed with real broker delivery. These results do not attest hosted CI or deployment.
