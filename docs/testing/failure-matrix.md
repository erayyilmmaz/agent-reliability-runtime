# Critical failure matrix

This matrix is the portfolio-level map from a reliability claim to an executable
test or repeatable local demonstration. A green local test suite is not a claim
that hosted CI, production deployment, or customer UAT has passed.

| Scenario | Expected durable behaviour | Automated evidence |
| --- | --- | --- |
| Happy path without provider credentials | API persists a run, outbox dispatches it, worker uses `DeterministicProvider`, run becomes `SUCCEEDED` | `scripts/compose-smoke.sh` |
| Duplicate request | Same client/key and same request return the original run | Compose smoke and `test_identical_duplicate_returns_original_run` |
| Duplicate broker delivery | A terminal or actively leased run is not executed again | Worker claim/lease state-machine tests |
| Timeout and retry | Timeout gets a retryable stable error code and bounded backoff | `test_worker_applies_attempt_timeout_and_records_retryable_code`, retry-policy tests |
| Provider fallback | A later attempt selects the next immutable `provider_order` provider | Provider-order and retry-policy tests; live fallback rehearsal is documented in run history |
| Worker crash / expired lease | Lease expiry records the failed attempt and schedules recovery | Lease state-machine and retry-policy tests |
| Broker outage | Run and outbox intent remain durable until confirmed publication | Transactional-outbox ADR and dispatcher topology tests |
| Poison message / DLQ | Invalid message is rejected without requeue to the durable dead-letter route | `test_worker_rejects_malformed_message`, RabbitMQ topology tests |
| Evaluation regression | Evaluation may be `FAILED` or `ERROR` while execution stays `SUCCEEDED` | Compose smoke, evaluation engine, API contract, and regression fixture tests |
| Immutable replay | New queued run has `replay_of_run_id`; repeat is idempotent | Compose smoke and `test_replay_creates_a_new_run_and_is_idempotent` |
| Invalid credentials / abuse | 401/403/429 precede run persistence and provider execution | `test_api_security_boundary.py` |

## Running the evidence

```bash
make test
make lint
make coverage
make smoke
```

`make smoke` starts the local Compose stack, applies migrations, submits a
credentials-free deterministic run, waits for success, then stops the stack.
For an interactive demo, use `make dev` instead and open Grafana at
`http://localhost:3000`; the provisioned **Agent Reliability Runtime** dashboard
shows runtime counters from Prometheus.

The CI coverage gate starts at 60% line coverage. The Compose smoke job is kept
separate because it validates the real service boundaries that unit coverage
cannot exercise safely without a running PostgreSQL, RabbitMQ, and Redis stack.
