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

ARR-3 establishes durable PostgreSQL entities for runs, attempts, events,
outbox delivery intent, and evaluations. It also makes execution transitions
explicit and rejects illegal state changes. The next milestone is `POST /runs`
returning `202 Accepted` with idempotent submission.

## Source-of-truth documents

- [V0 product and reliability contract](docs/architecture/v0-contract.md)
- [ADR-0001: transactional outbox](docs/adr/0001-transactional-outbox.md)
- [ADR-0002: API and worker separation](docs/adr/0002-api-worker-separation.md)
- [ADR-0003: run, attempt, and event records](docs/adr/0003-run-attempt-event-model.md)

## V0 technology direction

- Python, FastAPI, Pydantic, SQLAlchemy, Alembic
- PostgreSQL, Redis, RabbitMQ
- OpenTelemetry, Prometheus, Grafana, structured JSON logs
- pytest, pytest-asyncio, deterministic fake providers
- Docker, Docker Compose, GitHub Actions

## Local development

Prerequisites: Docker Desktop, Python 3.12+, and [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env
make dev
```

The Compose stack starts the API on `http://localhost:8000`, PostgreSQL, Redis,
RabbitMQ, OpenTelemetry Collector, Prometheus, and Grafana. API, worker,
outbox-dispatcher, and scheduler/recovery run as separate commands from the
same application image.

Use the following deterministic checks during development:

```bash
make test
make lint
make format-check
make migrate
make down
```

The API also exposes `GET /healthz` for process-level health checks.

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

Workers resolve the current attempt's provider from the immutable `provider_order`.
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
