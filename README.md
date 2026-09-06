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
