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

ARR-2 establishes the FastAPI project, four isolated runtime entry points,
configuration validation, migrations, local Docker Compose services, and the
initial deterministic test suite. The next milestone is durable run persistence
and `POST /runs` returning `202 Accepted`.

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

The API currently exposes `GET /healthz`; actual run submission is introduced
in ARR-4 after the ARR-3 persistence model is complete.
