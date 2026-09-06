# Recruiter Demo: 10–15 minutes

This guide demonstrates the reliability boundary without a provider credential
or an unverified failure claim. It uses the deterministic provider locally;
the failure scenarios point to their explicit automated evidence.

## 1. Start the runtime (2 minutes)

In one terminal, run:

```bash
make dev
```

The Compose stack starts PostgreSQL, Redis, RabbitMQ, migrations, API,
dispatcher, worker, scheduler, OpenTelemetry Collector, Prometheus, and
Grafana. Wait for the API to listen on port 8000.

## 2. Run the SDK tour (2 minutes)

In a second terminal, run:

```bash
make recruiter-demo
```

The Python client calls `healthz`, submits a durable run, waits for a terminal
state, then prints its persisted attempts and events. The `202 Accepted`
response means PostgreSQL has stored the run and transactional outbox intent;
it does not mean a provider has completed.

The client can also be used directly:

```python
from agent_runtime import AgentRuntimeClient

with AgentRuntimeClient(client_id="my-service") as client:
    submitted = client.run({"prompt": "hello"})
    run = client.wait_for_terminal(submitted.run_id)
    replay = client.replay(run.run_id)
```

For caller-controlled recovery after a client-side transport error, pass an
explicit `idempotency_key` to `run()` or `replay()` and reuse that same key.

## 3. Inspect observability (2 minutes)

Open [Grafana](http://localhost:3000) and select **Agent Reliability Runtime**.
The dashboard shows submitted runs, attempt outcomes, scheduled retries, and
outbox dispatch outcomes. Prometheus is available at
[localhost:9090](http://localhost:9090).

## 4. Inspect the failure evidence (3 minutes)

The credentials-free demo intentionally completes successfully. Do not infer a
retry or fallback from that success. Instead, inspect the verified scenario
matrix in [failure-matrix.md](testing/failure-matrix.md), then run:

```bash
uv run pytest \
  tests/unit/test_worker_message.py::test_worker_applies_attempt_timeout_and_records_retryable_code \
  tests/unit/test_retry_policy.py \
  tests/unit/test_outbox_dispatcher.py
```

These cover timeout classification, retry policy, and durable queue/dead-letter
topology. The run and event records shown by the demo are the operational place
where attempts, retries, fallback, recovery, and terminal reasons are audited.

## Architecture and trade-offs

```text
SDK / caller -> FastAPI -> PostgreSQL Run + Outbox (one transaction)
                              -> dispatcher -> RabbitMQ -> worker -> provider
```

- **FastAPI vs worker:** submission stays fast and durable; slow/provider work
  cannot hold an HTTP request open.
- **PostgreSQL outbox vs direct publish:** a broker outage cannot lose an
  accepted run, but duplicate publish is possible after confirmation races.
- **RabbitMQ at-least-once:** workers must be idempotent; exactly-once is not
  claimed.
- **Redis:** API-key deployments use it for a shared fixed-window rate limit;
  it is not the source of truth for run state.
- **Retries and fallback:** the immutable run policy controls bounded retry,
  provider order, and timeout handling. A failure can still end as `FAILED` or
  `DEAD_LETTERED`; reliability means explainable recovery, not guaranteed
  success.

For the full data and state contract, continue with
[v0-contract.md](architecture/v0-contract.md) and the linked ADRs.
