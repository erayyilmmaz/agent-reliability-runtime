# V0 Product and Reliability Contract

**Status:** Accepted baseline

**Scope:** ARR-1 — Product Contract, Reliability Semantics and Architecture Baseline

## 1. Product boundary

Agent Reliability Runtime turns an agent or LLM invocation into a durable,
asynchronous **run**. Its responsibilities are durable acceptance, delivery,
execution coordination, failure handling, provider isolation, and operational
evidence.

V0 does **not** implement an agent framework, arbitrary user-code execution,
a workflow DSL, a frontend, RAG/vector search, SaaS billing, multi-tenancy,
complex RBAC, Kubernetes, Terraform, or a streaming/WebSocket gateway.

## 2. Acceptance contract

The future `POST /runs` endpoint has one responsibility:

```text
validate request -> persist Run + Outbox event atomically -> 202 Accepted
```

It must not call a model or tool provider in the request process. A successful
`202` means that the run and its delivery intent are durable; it does not mean
the provider has started or completed execution.

Required response fields are `run_id`, `execution_status`, and an API location
for reading the run. The initial status is `QUEUED`.

## 3. Idempotent submission

Clients provide an `Idempotency-Key` on every create-run request. The key is
scoped to the authenticated caller and the create-run operation.

| Situation | Required result |
| --- | --- |
| First valid key/request pair | Create one run and one outbox event. |
| Same key and semantically identical request | Return the original accepted run; do not create a new event. |
| Same key with a different canonical request fingerprint | Reject with `409 Conflict`; never silently reuse the original run. |
| Invalid request | Do not reserve the key or create a run. |

The persistence model must retain the key, caller scope, canonical request
fingerprint, run identifier, creation time, and expiry policy. The first
implementation uses a 24-hour retention window; changing that window is a
compatibility decision and requires an ADR.

## 4. Execution lifecycle

`execution_status` is authoritative for execution only.

| Status | Meaning | Terminal | Allowed next status |
| --- | --- | --- | --- |
| `QUEUED` | Run and outbox delivery intent are durable. | No | `RUNNING`, `FAILED` |
| `RUNNING` | A worker holds a valid execution lease. | No | `SUCCEEDED`, `RETRY_SCHEDULED`, `FAILED`, `DEAD_LETTERED` |
| `RETRY_SCHEDULED` | A retry has been scheduled with backoff. | No | `RUNNING`, `FAILED`, `DEAD_LETTERED` |
| `SUCCEEDED` | An attempt completed and its result passed technical completion checks. | Yes | none |
| `FAILED` | Execution cannot continue under policy. | Yes | none |
| `DEAD_LETTERED` | Delivery or execution exhausted its retry policy and requires intervention. | Yes | none |

`QUEUED -> FAILED` is reserved for validation or dispatch failures discovered
after persistence. Terminal runs are immutable. A replay creates a new run
with `replay_of_run_id`; it never alters the old run.

## 5. Evaluation lifecycle

Evaluation is independent from execution. `evaluation_status` is one of
`NOT_RUN`, `PENDING`, `PASSED`, `FAILED`, or `ERROR`.

For example, an execution can be `SUCCEEDED` while evaluation is `FAILED`.
Evaluation evidence must be stored independently and must not rewrite the
execution outcome.

## 6. Delivery and recovery semantics

RabbitMQ delivery is at-least-once. The system deliberately does not claim
exactly-once delivery or exactly-once provider side effects.

- The API commits a run and an outbox event in the same PostgreSQL transaction.
- An outbox dispatcher publishes after commit and records publish progress.
- Consumers use manual acknowledgement only after durable processing progress.
- Workers use an execution lease; an expired lease is recoverable by another
  worker according to the retry policy.
- Duplicate messages and worker restarts are normal scenarios, not exceptional
  product states.
- Provider calls receive a stable run/attempt correlation token where the
  provider supports idempotency. Where it does not, the residual duplicate
  side-effect risk is recorded rather than hidden.

## 7. Error taxonomy and policy

The initial policy matrix is intentionally explicit. Provider adapters map
provider-specific failures into these categories.

| Error code | Retry | Provider fallback | Terminal for current attempt | Metric category |
| --- | --- | --- | --- | --- |
| `PROVIDER_TIMEOUT` | Yes | Yes | Yes | `provider_timeout` |
| `PROVIDER_RATE_LIMITED` | Yes | Yes | Yes | `provider_rate_limited` |
| `PROVIDER_UNAVAILABLE` | Yes | Yes | Yes | `provider_unavailable` |
| `PROVIDER_SERVER_ERROR` | Yes | Yes | Yes | `provider_server_error` |
| `PROVIDER_AUTH_ERROR` | No | No | Yes | `provider_auth_error` |
| `PROVIDER_BAD_REQUEST` | No | No | Yes | `provider_bad_request` |
| `OUTPUT_VALIDATION_ERROR` | Policy-dependent | Policy-dependent | Yes | `output_validation_error` |
| `INTERNAL_ERROR` | Yes, bounded | No by default | Yes | `internal_error` |
| `EXECUTION_LEASE_EXPIRED` | Yes | No | Yes | `execution_lease_expired` |

“Terminal for current attempt” means the attempt closes with an outcome. It
does not necessarily make the run terminal: retryable errors move the run to
`RETRY_SCHEDULED` when budget remains. Retry count, maximum elapsed time,
timeout, and fallback policy are per-run execution policy and will be persisted
with the run.

## 8. Data ownership and auditability

The model uses three immutable concepts:

- **Run:** the accepted unit of work and its current execution/evaluation
  summaries.
- **Attempt:** one concrete execution claim, provider selection, timing, and
  outcome.
- **Event:** a time-ordered operational fact, such as queued, leased,
  provider-called, retry-scheduled, or completed.

Current status may be denormalized for efficient reads, but history remains
append-only. Attempts and events explain fallback, retries, and recovery
without inventing provider-specific run statuses.

## 9. Provider boundary

The runtime owns execution semantics; provider adapters own request translation,
provider error mapping, capability declaration, and safe response normalization.
Provider fallback is represented by attempts and events, never by statuses such
as `OPENAI_FAILED` or `FALLBACK_PROVIDER`.

V0 includes a deterministic `FakeProvider` and at least one real provider
adapter. Provider credentials, authorization headers, raw prompts, and raw
model responses must not be emitted to logs, metrics, or traces by default.
Run and attempt identifiers may be emitted as correlation attributes.

## 10. Observability and non-goals

Every run/attempt transition must be observable through structured logs,
metrics, and traces without sensitive payloads. The later observability work
must preserve `run_id` and `attempt_id` correlation across API, dispatcher,
queue, and worker boundaries.

This contract is the source of truth for V0 behaviour. An intentional change to
delivery guarantees, lifecycle semantics, idempotency scope, or data
immutability requires an ADR and compatible migration plan.
