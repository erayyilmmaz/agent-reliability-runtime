# Agent Reliability Runtime

Provider-agnostic AI agent jobs for services that need durable, observable,
asynchronous execution. The runtime accepts a request, persists the run and an
outbox event in one database transaction, and returns `202 Accepted`. Workers
then execute the work outside the API process.

## V0 in one sentence

V0 is a reliability runtime for agent and LLM work; it is not an agent
framework, workflow builder, or frontend product.

## Architecture

```text
Application / Agent
        |
        v
     REST API  -- persist Run + Outbox atomically --> PostgreSQL
        |                                                |
        '-- 202 Accepted                                v
                                                 Outbox dispatcher
                                                        |
                                                        v
                                                    RabbitMQ
                                                        |
                                                        v
                                                     Worker
                                                        |
                                                        v
                                             Model / Tool provider
```

The queue is **at-least-once**. Duplicate delivery is expected and is handled
by idempotent worker processing; V0 does not claim exactly-once execution.

## Current milestone

V0 is feature-complete as a portfolio-quality reliability runtime: durable
submission, transactional outbox, leased worker execution, retry/dead-letter
handling, provider fallback, evaluation/replay, observability, and an optional
production API boundary are implemented. ARR-12 adds the clean-checkout CI,
Compose smoke demo, critical failure matrix, and contributor delivery material.

## Source-of-truth documents

- [V0 product and reliability contract](docs/architecture/v0-contract.md)
- [ADR-0001: transactional outbox](docs/adr/0001-transactional-outbox.md)
- [ADR-0002: API and worker separation](docs/adr/0002-api-worker-separation.md)
- [ADR-0003: run, attempt, and event records](docs/adr/0003-run-attempt-event-model.md)
- [ADR-0004: API security boundary](docs/adr/0004-api-security-boundary.md)
- [Policy-aware provider routing](docs/architecture/routing-policy.md)
- [Evaluation regression suite](docs/architecture/evaluation-regression-suite.md)
- [Least privilege: secrets, Kubernetes, database](docs/security/least-privilege.md)
- [10–15 minute recruiter demo](docs/recruiter-demo.md)
- [Critical failure matrix](docs/testing/failure-matrix.md)

## V0 technology direction

- Python, FastAPI, Pydantic, SQLAlchemy, Alembic
- PostgreSQL, Redis, RabbitMQ
- OpenTelemetry, Prometheus, Grafana, structured JSON logs
- pytest, pytest-asyncio, deterministic fake providers
- Docker, Docker Compose, GitHub Actions
- Kubernetes, Helm (API and worker processes scale independently)

## Local development

Prerequisites: Docker Desktop, Python 3.12+, and [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env
make dev
```

The Compose stack starts PostgreSQL, Redis, and RabbitMQ; runs Alembic
migrations to completion; then starts the API on `http://localhost:8000`,
OpenTelemetry Collector, Prometheus, Grafana, worker, outbox-dispatcher, and
scheduler/recovery. Application processes never start against an unmigrated
database.

Use the following deterministic checks during development:

```bash
make test
make lint
make format-check
make down
```

The API also exposes `GET /healthz` for process-level health checks.

## Recruiter demo and Python client SDK

Start `make dev` in one terminal, then run `make recruiter-demo` in another.
The [recruiter demo](docs/recruiter-demo.md) uses the public
`AgentRuntimeClient` to submit a run, wait for it, and inspect durable attempts
and events. It then points to the Grafana dashboard and verified failure matrix.
The SDK exposes `run`, `get_run`, `replay`, attempt/event reads, and
`wait_for_terminal`; it sends `X-Client-Id`, idempotency, and optional API-key
headers consistently.

## Kubernetes deployment

The Helm chart deploys the API, worker, dispatcher, scheduler and migration Job
as separate workloads. Configuration is a ConfigMap; credentials come only from
pre-existing Kubernetes Secrets, split by role: the provider key reaches the
worker alone, and the DDL credential reaches the migration Job alone. Every pod
runs non-root with a read-only root filesystem, all capabilities dropped and no
service-account token. Background workloads are probed by a heartbeat that is
written only after real progress, not by checking that PID 1 exists. See
[least privilege](docs/security/least-privilege.md). See the
[kind/k3d deployment guide](docs/deployment/kubernetes.md) for a local cluster
demo, independent worker scaling and probe verification.

## Infrastructure as code

Terraform manages a small, provider-neutral Kubernetes application-plane
baseline: one namespace and a resource quota per `sandbox`, `staging`, or
`production` environment. It deliberately does not create clusters or managed
datastores without an account-owner decision. State backend settings, real
Terraform variables, and runtime Secret values are all outside Git.

```text
Remote Terraform state -> namespace + resource quota -> pre-provisioned Secret
                                                        |
                                                        v
                                               Helm runtime release
```

See the [Terraform deployment baseline](docs/deployment/terraform.md) for
environment boundaries, state/secret handling, and the reviewed apply flow.

## Five-minute credentials-free demo

The default local provider is deterministic, so this demo needs no OpenAI or
other provider credential. It proves the entire API → PostgreSQL outbox →
RabbitMQ → worker path.

```bash
make smoke
```

For an interactive session, run `make dev`, wait until the migration service
has completed, then submit a run:

```bash
curl -X POST http://localhost:8000/v1/runs \
  -H 'Content-Type: application/json' \
  -H 'X-Client-Id: local-demo' \
  -H 'Idempotency-Key: local-demo-run-0001' \
  --data '{"input":{"prompt":"hello"}}'
```

Poll the returned run through `GET /v1/runs/{run_id}`. Grafana is available at
`http://localhost:3000` and the provisioned **Agent Reliability Runtime**
dashboard reads metrics from Prometheus at `http://localhost:9090`.

## Run API

`POST /v1/runs` accepts a durable run and immediately returns `202 Accepted`.
It never calls a provider or waits for RabbitMQ delivery. Every request must
include `X-Client-Id` and an `Idempotency-Key` (8–255 URL-safe characters).

```bash
curl -X POST http://localhost:8000/v1/runs \
  -H 'Content-Type: application/json' \
  -H 'X-Client-Id: local-demo' \
  -H 'Idempotency-Key: local-demo-run-0001' \
  --data '{"input":{"prompt":"hello"}}'
```

Repeating a semantically identical request with the same client/key returns the
original run. Reusing that key with a different body returns `409 Conflict`.
Run, attempt, and event reads are available at `GET /v1/runs/{run_id}`, `GET
/v1/runs/{run_id}/attempts`, and `GET /v1/runs/{run_id}/events`.

## Reliable dispatch

The API transaction creates a Run, `RUN_QUEUED` event, and outbox event
together. The separate dispatcher locks an unpublished event, publishes a
minimal persistent RabbitMQ message, waits for publisher confirmation, then
marks `published_at`. A broker outage leaves the run and outbox event durable;
the dispatcher retries it later. A crash after broker confirmation but before
the database update may publish a duplicate, so consumers must be idempotent.

## Worker leases

Workers consume with manual acknowledgement. They atomically claim a queued run,
create a numbered attempt, and record a worker-owned execution lease before
executing it. Terminal runs and deliveries held by another active lease are
acknowledged without a second execution. A successful durable state update
precedes the acknowledgement. The scheduler marks expired leases as
`RETRY_SCHEDULED`, preserving the failed attempt for later retry policy.

## Retry and dead-letter delivery

Each accepted run stores a resolved retry policy in its immutable policy snapshot:
`max_attempts`, `attempt_timeout_seconds`, `initial_backoff_seconds`,
`max_backoff_seconds`, and `provider_order`. Timeouts, rate limits, provider 5xx
responses, temporary unavailability, and expired leases are retried with bounded
exponential backoff. Authentication and other client failures finish immediately.

The scheduler persists `next_attempt_at` and converts only due runs into a fresh
transactional outbox event, so a scheduler restart cannot lose a retry. A run that
uses its retry budget becomes `DEAD_LETTERED`; its history is retained in PostgreSQL
and a `RUN_DEAD_LETTERED` event is published to the durable
`agent_runtime.dead_letter` queue. Malformed execution messages are rejected without
requeue and are routed to that same queue as poison messages.

The ARR-7 execution queue is `agent_runtime.execution.v2` because RabbitMQ queue
arguments are immutable. Workers also drain the prior `agent_runtime.execution` queue
without binding new deliveries to it, so an upgrade does not strand ARR-6 messages.

## Provider adapters

The acceptance API turns optional `policy.routing` candidates and a strategy
(`lowest_latency`, `lowest_cost`, `quality_first`, or `balanced`) into an
explainable, immutable `provider_order`; see the [routing policy
contract](docs/architecture/routing-policy.md). Workers resolve the current
attempt's provider from that immutable order.
`deterministic` remains the default safe local adapter. The first real adapter is
OpenAI Responses: set `APP_OPENAI_API_KEY` only in the worker environment and submit
`policy.provider_order: ["openai"]`. Its input requires either `input.prompt` or
`input.messages`; optional `policy.model`, `policy.instructions`, and
`policy.max_output_tokens` are allowlisted. The adapter calls `/v1/responses` with
`store: false`, stores only response ID/model/text/token counts, and never persists a
provider error body or API key. HTTP 429 and 5xx responses preserve their status for
the retry classifier; 401/403 and malformed provider input finish without retry.

## Observability

Every process exports OTLP traces and metrics to the bundled collector. The API creates
the root HTTP span, stores only W3C `traceparent`/`tracestate` in the transactional
outbox, and the dispatcher, worker, database work, and provider adapter continue that
same trace. Trace attributes may contain `run_id` and `attempt_id` for debugging, but
metrics deliberately never use either as a label. Metric dimensions are restricted to
known providers, controlled error codes, outcomes, and event types.

Docker Compose provisions Prometheus at `http://localhost:9090` and a read-only Grafana
dashboard at `http://localhost:3000`. The JSON logger has an allowlist (`event`, run and
attempt IDs, provider, error code, trace/span IDs), so prompt, input, response and secret
fields cannot reach logs. OTLP instrumentation never captures HTTP bodies or headers.

## Evaluation and replay

`POST /v1/runs/{run_id}/evaluations` accepts evaluation of a `SUCCEEDED` run's
persisted result and returns **202 / PENDING**, not a completed evaluation.
`details.job_run_id` identifies durable outbox/worker work; poll
`GET /v1/jobs/{job_run_id}` for completion. The request contains at most 16
ordered rules: `non_empty`, restricted `json_schema`, `latency_budget`, or generic
`rule` (`exists`, `equals`, `not_equals`, `contains`). Regex `matches` and unsafe
schema constructs are rejected. Results are deterministic, stored in the
`evaluations` lifecycle record, and available from
`GET /v1/runs/{run_id}/evaluations`. An evaluation may be `FAILED` or `ERROR`
without changing the run's execution result.

`POST /v1/runs/{run_id}/replay` requires a fresh `Idempotency-Key` and creates
a distinct `QUEUED` run whose `replay_of_run_id` points to the original. It
deep-copies the original immutable input and resolved policy snapshot, writes
audit events to both histories, and queues the new run through the regular
transactional outbox. Repeating the same replay request with the same client
and key returns that replay rather than creating another execution.

## Evaluation regression suite

Use the versioned [evaluation regression suite](docs/architecture/evaluation-regression-suite.md)
to compare a baseline and candidate provider/model on the same deterministic
rules. It emits a machine-readable report with independent quality, latency,
and estimated-cost deltas and can be triggered by CLI or
`POST /v1/evaluation-regressions`.

The regression API requires an authenticated credential and `Idempotency-Key`,
including when the rest of the API runs in anonymous local mode (where this
endpoint returns `401`). It returns a durable `202` job receipt; read the final
report from `GET /v1/jobs/{job_id}`. Defaults allow 10 cases and 20 provider calls
per job. The operator CLI remains synchronous and is not an untrusted API boundary.

## Resource controls

See [SEC-E2 resource limits and upgrade contract](docs/security/resource-controls.md)
for policy bounds, per-principal/tenant quotas, safe rules, and the migration.
History endpoints (`/attempts`, `/events`, `/evaluations`) now return at most
50 records by default; use `limit=1..100` and the `X-Next-Cursor` response header
as the next request's `cursor`. SDK callers can use `get_history_page()`;
`get_attempts()` and `get_events()` return only the first page.

## API security boundary

Authentication defaults to `api_key` and the environment defaults to `production`.
Startup requires `APP_AUTH_CREDENTIALS` (a server-controlled credential registry)
and an independent `APP_AUTH_PEPPER`. Each generated key maps to a principal
and tenant. Send it in `X-API-Key` or `Authorization: Bearer <key>`; `X-Client-Id`
is optional, validated, and does not select a tenant in authenticated mode.
Missing credentials return `401`; invalid credentials return `403`. An atomic
Redis fixed-window limit applies across API instances and rotated keys per
principal, including routes without a client header. It returns `429` before
a run is persisted or a provider can be called; Redis failure returns `503`.

Only `GET /healthz` is public in API-key mode. `/docs`, `/redoc`, and
`/openapi.json` are disabled. See [credential provisioning, rotation and legacy
migration](docs/security/credentials.md) before upgrading an existing installation.
The old `APP_AUTH_API_KEY_HASH` setting is no longer supported.

Security audit records are append-only PostgreSQL facts for authentication and
rate-limit outcomes. New records contain controlled event metadata, the verified
tenant ID, and a short credential-record fingerprint, never the key or its KDF
verifier. Unauthenticated identity assertions are not stored. Anonymous operation
requires both `APP_ENVIRONMENT=local` and `APP_AUTH_MODE=disabled`, as explicitly
configured in local Compose. Local mode still requires a valid client header
for run-scoped operations. Environment files are not loaded automatically by
the Python process; export these variables when running local commands directly.

## Audit and data protection

SEC-E3 adds fail-closed audit persistence, sensitive-read audit, bounded trace
context, sanitized exception diagnostics, and security alert rules. Deterministic
provider echo is now **off by default**; operator-only
`APP_DETERMINISTIC_ECHO_INPUT=true` enables input echo, never policy echo.
See [audit, telemetry and data-protection contract](docs/security/audit-telemetry-data.md).
The tenant envelope-encryption/retention adapter is a tested **prototype**, not
automatic encryption of existing run data; production KMS and runtime read/write
binding remain a separate rollout gate, as agreed for this step.

## Delivery checks

The GitHub Actions workflow runs locked dependency installation, Ruff, mypy,
pytest with coverage, and an isolated Docker Compose smoke demo on pushes and
pull requests. See the [critical failure matrix](docs/testing/failure-matrix.md)
for the exact scenario-to-evidence mapping.

## License

Copyright 2026 Eray Yılmaz. Distributed under the
[Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for project attribution.
