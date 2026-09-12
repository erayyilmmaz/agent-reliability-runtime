# Performance test suite

Reproducible k6 scenarios for the Agent Reliability Runtime, plus the
environment overlay that makes a local Compose stack behave like the production
configuration.

Results and analysis: [`docs/performance-audit.md`](../docs/performance-audit.md).

---

## Why these scenarios look the way they do

Each scenario models a **caller journey**, not an endpoint. A real integration
uses `AgentRuntimeClient`: it submits a run, polls until the run reaches a
terminal state, then reads the durable attempt and event history. Looping one
URL at maximum rate measures that URL, not the system, and produces numbers no
capacity decision can be based on.

Think time is therefore deliberate, not padding. `load/load.js` waits
500-1500 ms between journeys because a caller does too.

---

## Requirements

| Tool | Version used | Purpose |
| ---- | ------------ | ------- |
| k6 | v2.2.0 | Load generation |
| Docker + Compose | - | The system under test |
| uv | 0.11+ | Generates the throwaway credential |

```bash
brew install k6          # or see https://grafana.com/docs/k6/latest/set-up/install-k6/
uv sync --dev
```

---

## Safe-environment requirements

**These scenarios must never be pointed at production or at a shared staging
environment.** They create durable runs, consume provider-call quota, and write
audit rows that cannot be deleted (the audit and event tables are append-only
by database trigger).

The intended target is the disposable local Compose stack. `stress` and `spike`
in particular drive the API into saturation on purpose.

The stack binds every port to `127.0.0.1` (SEC-021), so it is not reachable
from the network while the tests run.

---

## Setup

```bash
# 1. Generate a throwaway credential registry (writes performance-tests/.env.perf).
bash performance-tests/scripts/setup-env.sh

# 2. Start the stack in the production-shaped configuration.
set -a; . performance-tests/.env.perf; set +a
docker compose -f docker-compose.yml \
               -f performance-tests/docker-compose.perf.yml up -d --build

# 3. Confirm the authenticated path works.
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "X-API-Key: $ARR_API_KEY" \
  http://localhost:8000/v1/runs/00000000-0000-0000-0000-000000000000   # expect 404
```

`.env.perf` is gitignored, written `0600`, and contains a key valid only for
this disposable stack. **Never commit it and never reuse it.**

---

## Environment variables

| Variable | Default | Meaning |
| -------- | ------- | ------- |
| `ARR_BASE_URL` | `http://localhost:8000` | System under test |
| `ARR_API_KEY` | *(from `.env.perf`)* | Empty string exercises the `disabled`-auth path |
| `ARR_CLIENT_ID` | `perf-tenant` | Tenant header |
| `ARR_VUS` | per scenario | Concurrency |
| `ARR_DURATION` | per scenario | Test window |
| `ARR_RUN_ID` | - | Required by `scripts/read-sweep.js` |

The overlay also exposes `ARR_PERF_*` variables for quota and rate-limit
ceilings. They default to the **maximum `Settings` accepts**, so a load test
measures the system rather than its admission controls. Those ceilings are
themselves analysed as PERF-008 in the report.

---

## Running

```bash
set -a; . performance-tests/.env.perf; set +a

# Always first: validates the script and the stack under minimal load.
k6 run performance-tests/smoke/smoke.js

# Average load. ARR_VUS drives the concurrency sweep.
ARR_VUS=10 ARR_DURATION=60s k6 run performance-tests/load/load.js

# Ramp to the capacity knee.
k6 run performance-tests/stress/stress.js

# Burst and recovery.
k6 run performance-tests/spike/spike.js

# Leak and drift hunting. Use 1h+ in a dedicated environment.
ARR_DURATION=10m k6 run performance-tests/soak/soak.js
```

### Isolating a single tier

`scripts/read-sweep.js` and `scripts/submit-sweep.js` remove think time and the
worker pipeline so the API's own service curve is visible:

```bash
RUN_ID=$(curl -s -X POST http://localhost:8000/v1/runs \
  -H 'Content-Type: application/json' -H "X-API-Key: $ARR_API_KEY" \
  -H "Idempotency-Key: seed-$(date +%s)" \
  --data '{"input":{"prompt":"seed"}}' | sed -nE 's/.*"run_id":"([^"]+)".*/\1/p')

for vus in 1 5 10 25 50; do
  ARR_VUS=$vus ARR_DURATION=20s ARR_RUN_ID=$RUN_ID \
    k6 run --quiet --summary-export=results/read-$vus.json \
    performance-tests/scripts/read-sweep.js
done
```

### Attributing latency to authentication

The single most useful comparison in this suite. Same stack, same scenario, one
variable:

```bash
docker compose -f docker-compose.yml -f performance-tests/docker-compose.perf.yml \
  run -d --rm --name arr-api-noauth -p 8001:8000 \
  -e APP_AUTH_MODE=disabled -e APP_ENVIRONMENT=local --no-deps api

ARR_BASE_URL=http://localhost:8001 ARR_API_KEY= k6 run performance-tests/smoke/smoke.js
```

---

## Expected output

`smoke` should be fully green:

```
✓ submit accepted (202)   ✓ read run (200)
✓ read attempts (200)     ✓ read events (200)
checks_succeeded: 100.00%   http_req_failed: 0.00%
```

Indicative figures from the reference run (see the report for the environment):

| Scenario | Observation |
| -------- | ----------- |
| smoke (1 VU, authenticated) | submit p50 ≈ 104 ms, read p50 ≈ 88 ms |
| smoke (1 VU, auth disabled) | submit p50 ≈ 18 ms, read p50 ≈ 7 ms |
| read sweep | peaks ≈ 71 RPS at 10 VUs, then degrades |
| submit sweep | peaks ≈ 53 RPS at 10 VUs |

A run that differs substantially is a signal — either the environment differs
or something changed. Compare like with like (see "Before/after methodology"
in the report).

---

## Custom metrics

Beyond k6's built-ins, the suite records per-stage trends so a slow journey can
be attributed to one call rather than to "the API":

| Metric | Meaning |
| ------ | ------- |
| `arr_submit_latency` | `POST /v1/runs` |
| `arr_read_latency` | `GET /v1/runs/{id}` |
| `arr_attempts_latency` / `arr_events_latency` | history reads |
| `arr_time_to_terminal` | submit → terminal state, end to end |
| `arr_submit_rejected` / `arr_quota_rejected` / `arr_rate_limited` | admission outcomes |

---

## Test data

No fixtures are shipped. Each run creates its own data through the public API,
so the suite never depends on database state it did not produce.

This does mean **state accumulates across runs**: runs, events and audit rows
are append-only. Reset between measurement campaigns, or comparisons will drift
as table sizes grow:

```bash
docker compose -f docker-compose.yml -f performance-tests/docker-compose.perf.yml down -v
```

---

## Load generator validation

k6 must not be the bottleneck. During the reference run the generator stayed
far below saturation while the API container sat at ~850% CPU. Before trusting
any result, confirm the generator is idle relative to the target:

```bash
docker stats --no-stream      # API CPU should dominate
```

If k6 itself saturates a core, reduce VUs per process or distribute the load.

---

## Thresholds

`smoke` enforces correctness thresholds (all checks pass, zero failed
requests). The load scenarios deliberately **record rather than enforce**
latency: this repository has no declared SLO, and inventing one would turn a
measurement into an unfounded assertion.

Once a baseline is agreed, convert it into gates — see "Performance quality
gates" in the report.
