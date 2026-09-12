# Performance & Scalability Audit

**Target:** Agent Reliability Runtime (`agent-reliability-runtime` v0.1.0)
**Revision:** `743a714` (branch `main`) — PERF-001 remediated, see its Resolved block
**Date:** 2026-09-12
**Type:** Measurement-driven performance and scalability audit
**Method:** Measure → Analyze → Diagnose → Prioritize → Recommend
**Application code was not modified.**

---

## 1. Executive Summary

| Area | Result |
| ---- | ------ |
| **Overall performance health** | **Constrained by one control, otherwise efficient** |
| **Main bottleneck** | ~~Per-request PBKDF2 credential verification~~ → **resolved**; now a single-core API event loop |
| **Current capacity** | ~71 RPS reads / ~53 RPS submits, single API replica, 10 cores |
| **Primary latency source** | Authentication — 92% of an authenticated read's wall time |
| **Primary memory risk** | None found. +1.5 MiB under load, full recovery |
| **Primary DB risk** | Unbounded, unindexed `security_audit_events`; 2 writes per read |
| **Primary frontend risk** | **Not applicable** — JSON API only, no frontend exists |
| **Scalability risk** | CPU-bound per request; horizontal scaling works but is expensive |
| **Highest priority fix** | ~~PERF-001~~ **done**. Next: multi-process/replica scaling, then PERF-004 |

### The one-paragraph version

This system spends its time in exactly one place. A `GET /v1/runs/{id}` costs
**87.8 ms** at p50 with authentication enabled and **7.3 ms** with it disabled —
the same stack, the same query, one configuration variable. The database
portion of that request is **0.028 ms**. Everything else the runtime does is
efficient: the hot query is a two-buffer index scan, memory is flat under load,
the outbox never backs up, pagination is in place, and payloads are under 1.3 KB.
The capacity ceiling is not a database or an I/O limit; it is the CPU cost of
deriving a PBKDF2 verifier 600,000 iterations deep on every single request.

That cost is a direct consequence of the SEC-010 remediation in the security
audit, which replaced an unsalted SHA-256 verifier with a proper KDF. The
remediation was correct. Applying it per request rather than once per session
is what turned a security improvement into the system's capacity limit.

---

## 2. Test Environment

| Component | Detail |
| --------- | ------ |
| Host | Apple Silicon, 10 cores, 24 GB RAM, macOS |
| Container runtime | Docker Desktop, linux/arm64, ~7.65 GiB allocated to the VM |
| System under test | Local Compose stack, `docker-compose.yml` + `performance-tests/docker-compose.perf.yml` |
| API image | `python:3.14-slim`, Python 3.14.7, uvicorn 0.52, single process |
| Database | PostgreSQL 17-alpine |
| Cache / broker | Redis 8.1.0 / RabbitMQ 4 |
| Load generator | k6 v2.2.0, same host |
| Auth mode | `api_key` (production shape) unless stated otherwise |
| OTel | Disabled during measurement so the collector is not in the path |

**Generator validation.** k6 ran on the same host as the target. At peak the API
container consumed ~850% CPU while k6 stayed well below saturation, so the
generator was not the limiting factor. Results at the very top of the sweep
(50 VUs) are nonetheless conservative: host and target share 10 cores.

### What this environment is and is not

This is a **single-host, single-replica** measurement. It establishes the
per-replica service curve and the per-request cost model, which is what
capacity planning needs. It does **not** measure: multi-replica behaviour,
real network latency, managed-database performance, cloud CPU quotas, or
provider (OpenAI) latency. Those are listed in §16.

---

## 3. Architecture & Discovery (Phase 0)

| Area | Finding |
| ---- | ------- |
| Application type | Asynchronous job runtime: REST API + 3 background processes |
| Backend | Python 3.14, FastAPI 0.141, Starlette, uvicorn (uvloop, httptools) |
| **Frontend** | **None.** JSON API only. No HTML, no bundle, no browser surface |
| Database | PostgreSQL 17 via SQLAlchemy 2.0 async + asyncpg 0.31 |
| ORM | SQLAlchemy 2.0, async session, no lazy loading |
| Cache | Redis 8.1 — used **only** for fixed-window rate limiting, not for data |
| Queue | RabbitMQ 4 via aio-pika 10.0, publisher confirms, manual ack |
| Infrastructure | Docker, Helm chart, Terraform namespace baseline |
| Observability | OpenTelemetry traces + 8 counters; structured JSON logs |
| External deps | OpenAI Responses API (worker only); `deterministic` provider is the default |
| Auth | Static API key registry, PBKDF2-HMAC-SHA256 verifier |
| Background processing | worker (AMQP consumer), dispatcher (outbox poller), scheduler (lease/retry) |
| CI/CD | GitHub Actions — **no performance gate exists** |

Sections of the audit brief covering frontend bundles, Core Web Vitals, browser
main-thread work, images/fonts and render-blocking resources are **Not
Applicable**: there is no frontend in this repository. Per the brief, they are
not forced.

---

## 4. Critical Performance Paths

Derived from `AgentRuntimeClient`, `examples/recruiter_demo.py` and the API
surface. The dominant real journey is *submit → poll → inspect*.

| # | Journey | Endpoints | Frequency profile |
| - | ------- | --------- | ----------------- |
| **CP-1** | **Submit a run** | `POST /v1/runs` | Once per unit of work. Write path: quota admission, advisory lock, run + event + outbox in one transaction |
| **CP-2** | **Poll to terminal** | `GET /v1/runs/{id}` | **Highest call volume.** `wait_for_terminal` polls every 500 ms until terminal — one submit generates many reads |
| **CP-3** | Inspect history | `GET .../attempts`, `.../events` | Once or twice per run, paginated |
| **CP-4** | Evaluate result | `POST .../evaluations` | Optional, per run |
| **CP-5** | Replay | `POST .../replay` | Rare |
| **CP-6** | Regression job | `POST /v1/evaluation-regressions` | Rare, now a durable job (see §9) |
| **CP-7** | Execution pipeline | outbox → dispatcher → AMQP → worker | Once per attempt, off the request path |

**CP-2 is the hot path by volume.** The SDK's own `wait_for_terminal` polls at
500 ms; a run that takes 4 seconds produces 8 reads for 1 submit. Any
per-request cost is therefore multiplied roughly 8:1 in real usage — which is
what makes PERF-001 severe rather than merely wasteful.

---

## 5. Baseline (1 VU, no contention)

k6 `smoke`, 5 journeys, authenticated. Full journey including polling.

| Operation | p50 | p90 | p95 | max |
| --------- | --: | --: | --: | --: |
| `POST /v1/runs` | 104.4 ms | 114.5 ms | 116.3 ms | 118.2 ms |
| `GET /v1/runs/{id}` | 87.8 ms | 103.1 ms | 106.1 ms | 106.4 ms |
| `GET .../attempts` | 73.0 ms | 80.6 ms | 82.2 ms | 83.8 ms |
| `GET .../events` | 74.3 ms | 77.2 ms | 77.3 ms | 77.4 ms |
| submit → terminal | 761 ms | 1.11 s | 1.12 s | 1.12 s |

Error rate 0.00%, checks 30/30.

**Read this table as a floor, not a result.** Every operation sits in a
66-105 ms band regardless of how much work it actually does. A run creation
(one transaction, three inserts, an advisory lock) costs 104 ms; reading one
row by primary key costs 88 ms. Work that differs by orders of magnitude
produces near-identical latency, which is the signature of a large fixed
per-request cost.

---

## 6. Request Lifecycle Attribution

The decisive experiment: identical stack, identical scenario, one variable
(`APP_AUTH_MODE`).

| Operation | `api_key` p50 | `disabled` p50 | Delta | Auth share |
| --------- | ------------: | -------------: | ----: | ---------: |
| `POST /v1/runs` | 104.4 ms | 17.8 ms | **−86.7 ms** | 83% |
| `GET /v1/runs/{id}` | 87.8 ms | 7.3 ms | **−80.5 ms** | **92%** |
| `GET .../attempts` | 73.0 ms | 8.8 ms | −64.2 ms | 88% |
| `GET .../events` | 74.3 ms | 5.8 ms | −68.6 ms | 92% |

Decomposing an authenticated read (p50 87.8 ms):

```
  GET /v1/runs/{id}  ──────────────────────────────────────── 87.8 ms
  ├─ PBKDF2 verifier derivation ............. 61.2 ms   (70%)   [measured]
  ├─ Redis rate-limit check + 2 audit INSERTs  ~19.3 ms (22%)   [by difference]
  ├─ Application + serialization ............. ~7.3 ms   (8%)   [auth-off baseline]
  └─ PostgreSQL query ........................ 0.028 ms (0.03%) [EXPLAIN ANALYZE]
```

Three independent measurements agree on the KDF figure:

1. **In-container microbenchmark:** `derive_verifier()` median **61.2 ms**
   (min 61.08, max 65.11, n=12) on linux/arm64 Python 3.14.7.
2. **Host microbenchmark:** median **89.5 ms** on macOS ARM Python 3.13.
3. **A/B delta:** 80.5 ms, consistent with 61.2 ms KDF plus Redis and audit work.

---

## 7. Capacity Curve

`scripts/read-sweep.js` — authenticated `GET /v1/runs/{id}`, no think time,
20 s per step.

| VUs | RPS | p50 | p90 | p95 | max | error % |
| --: | --: | --: | --: | --: | --: | ------: |
| 1 | 14.6 | 66.0 ms | 69.7 ms | 72.8 ms | 236.8 ms | 0.0 |
| 5 | 52.2 | 93.7 ms | 105.7 ms | 111.5 ms | 221.3 ms | 0.0 |
| **10** | **71.0** | 132.8 ms | 169.6 ms | 186.5 ms | 338.5 ms | 0.0 |
| 25 | 61.3 | 399.4 ms | 615.8 ms | 688.2 ms | 839.4 ms | 0.0 |
| 50 | 56.1 | 839.4 ms | 1311.1 ms | 1467.9 ms | 2816.4 ms | 0.0 |

Write path, `scripts/submit-sweep.js`:

| VUs | RPS | p50 | p95 | max |
| --: | --: | --: | --: | --: |
| 1 | 14.7 | 66.8 ms | 72.9 ms | 120.1 ms |
| 5 | 40.9 | 121.5 ms | 153.5 ms | 217.5 ms |
| **10** | **53.5** | 181.3 ms | 238.9 ms | 321.6 ms |
| 25 | 52.7 | 462.4 ms | 688.9 ms | 931.8 ms |

**Shape: saturation at 10 VUs, then retrograde.** Throughput does not plateau —
it *falls* by 21% from 71 to 56 RPS while latency grows 6.4×. That is
oversubscription: more concurrent KDF computations than cores, so added
concurrency buys context switching rather than work.

Error rate stays at 0.0% throughout. The system degrades by getting slower, not
by failing — which is the better failure mode, but it means **latency, not
errors, is the signal to alert on**.

```
RPS
 80│              ●71
   │         ●52      ╲
 60│                   ●61  ●56
   │                            
 40│    
   │  ●14.6
 20│ 
   └──┬────┬────┬────┬────┬──── VUs
      1    5   10   25   50
           ↑ knee      ↑ degradation
```

---

## 8. Resource Utilisation

`docker stats` during a 10 VU / 71 RPS read load:

| Container | CPU | Memory |
| --------- | --: | -----: |
| **api** | **849.6% / 719.2% / 848.6%** | 117.7 MiB |
| postgres | 9.1% / 11.7% / 8.3% | 52.9 MiB |
| redis | 0.9% / 1.7% / 1.8% | 4.1 MiB |

**The API burns ~8.5 cores to serve 71 reads per second** — about 120 ms of CPU
per request, for a query costing 0.028 ms. PostgreSQL, the component usually
blamed for latency, is at 9%.

### Why CPU parallelises (and why the event loop is *not* blocked)

`api/main.py:285` dispatches verification through
`await run_in_threadpool(authenticate_api_key, ...)`, and `hashlib.pbkdf2_hmac`
releases the GIL for the duration of the C-level computation. Verifications
therefore run genuinely in parallel across anyio's threadpool (15 threads
observed on PID 1).

**This is a correct design decision and should be preserved.** Had the KDF been
called directly in the async middleware it would have blocked the event loop
and capped the process at ~16 RPS — the single-core ceiling. Offloading is why
71 RPS is reachable at all.

It does not remove the cost; it only parallelises it. The ceiling becomes
`cores ÷ 61 ms` instead of `1 ÷ 61 ms`, and beyond the core count more
concurrency actively hurts.

---

## 9. Database Analysis

### Hot-path execution plans

```
-- CP-2, the highest-volume query
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM runs WHERE id=$1 AND client_id=$2;

 Index Scan using runs_pkey on runs (actual time=0.027..0.028 rows=1 loops=1)
   Index Cond: (id = '...'::uuid)
   Filter: ((client_id)::text = 'perf-tenant'::text)
   Buffers: shared hit=2
```

```
-- Quota admission on the submit path
 Aggregate (actual time=0.017..0.017 rows=1 loops=1)
   ->  Index Only Scan using ix_runs_principal_active on runs
         Index Cond: ((principal_id = ...) AND (execution_status = ANY (...)))
         Heap Fetches: 0
         Buffers: shared hit=1
```

**Both are optimal.** Two buffers, sub-0.03 ms, index-only scan with zero heap
fetches on the quota count. The index set added in SEC-E2 (`ix_runs_principal_active`,
`ix_runs_tenant_active`, `ix_*_page`) is doing its job.

### Where the database *is* weak

```
-- "Who accessed this run?" - the entire purpose of SEC-AUD-01 read auditing
EXPLAIN (ANALYZE, BUFFERS)
SELECT * FROM security_audit_events WHERE target_run_id=$1 ORDER BY created_at DESC LIMIT 50;

 Limit (actual time=4.575..4.580 rows=50 loops=1)
   ->  Sort  (Sort Method: top-N heapsort  Memory: 51kB)
         ->  Seq Scan on security_audit_events (actual time=0.036..3.426 rows=12694)
               Filter: (target_run_id = '...'::uuid)
               Rows Removed by Filter: 12785
               Buffers: shared hit=511
```

```
-- Retention purge
 Aggregate (actual time=2.669..2.669 rows=1)
   ->  Seq Scan on security_audit_events (Rows Removed by Filter: 25479)
         Filter: (created_at < (now() - '30 days'::interval))
         Buffers: shared hit=511
```

Sequential scans on both. The table carries indexes only on its primary key and
`client_id`; neither `target_run_id` nor `created_at` is indexed. At 25 k rows
this costs 4.6 ms. The relationship is linear in table size, and the table is
append-only with no purge job running — see PERF-005.

### Query budget per request

Measured by counting rows written and transactions committed:

| Operation | DB work |
| --------- | ------- |
| `GET /v1/runs/{id}` | 1 indexed SELECT + **2 audit INSERTs** |
| `POST /v1/runs` | advisory lock + 2 quota counts + run/event/outbox inserts (1 tx) + 2 audit INSERTs |

**Every read performs two writes.** 20 authenticated GETs produced exactly 40
rows in `security_audit_events` — one `AUTHENTICATION/ALLOWED` record and one
sensitive-read record, each in its own session and transaction
(`SqlAlchemySecurityAuditSink.record` opens a new session per record).

There is **no N+1 pattern anywhere.** Query count per request is constant and
does not grow with result-set size — list endpoints issue one parent check plus
one paginated query regardless of how many rows are returned.

### Table growth observed

| Table | Rows | Size |
| ----- | ---: | ---: |
| **security_audit_events** | **25,479** | **5,568 kB** |
| run_events | 106 | 136 kB |
| outbox_events | 27 | 48 kB |
| runs | 17 | 256 kB |

The audit table is three orders of magnitude larger than `runs` and is the only
table whose growth is driven by *read* traffic.

### Connection pool

No pool exhaustion observed at any concurrency level: zero errors, zero
timeouts, PostgreSQL CPU under 12% throughout. `create_async_engine` uses
SQLAlchemy's default pool (size 5, overflow 10) with `pool_pre_ping=True`. The
pool is not a bottleneck **at current throughput** — but see PERF-009: the
default is small relative to the threadpool width, and would bind before the
CPU does if the KDF cost were removed.

---

## 10. Pagination & Payloads

Pagination is implemented on all three history endpoints: `limit`
(`ge=1, le=100`, default 50) plus a `cursor` UUID — keyset pagination backed by
`ix_run_events_page`, `ix_run_attempts_page`, `ix_evaluations_page`. There is
**no unbounded list endpoint and no OFFSET pagination**, so the classic deep-page
degradation does not apply here.

Response sizes:

| Endpoint | Bytes |
| -------- | ----: |
| `GET /v1/runs/{id}` | 806 |
| `GET .../attempts` | 260 |
| `GET .../events` | 1,236 |
| `POST /v1/runs` (with `routing_decision`) | 617 |

All well under 1.3 KB. Serialization is not a measurable cost at this size, and
payload size is not correlated with latency in any of the runs.

No `Content-Encoding` is returned even when `Accept-Encoding: gzip, br` is sent.
At these sizes compression would be net-negative; it becomes relevant only if a
100-item event page is requested (see PERF-007).

---

## 11. Queue, Worker & Backpressure

| Signal | Observation |
| ------ | ----------- |
| Outbox backlog | **0 unpublished** of 10,737 total — the dispatcher keeps up |
| Worker claims | 2,058 `RUN_CLAIMED` for 2,058 runs — every run reached a worker |
| Attempts per run | 1.0 — no retry amplification |
| Poison handling | Malformed messages rejected without requeue |

### The backpressure finding

Of 2,058 runs submitted during the sweeps, **1,925 (93.5%) finished as `FAILED`
with `RESOURCE_QUOTA_EXCEEDED`** — and every one of them was rejected *at the
worker*, not at the API:

```
worker-1 | {"event":"WORKER_EXECUTION_FAILED","exception_type":"QuotaExceededError",
           "exception_frames":["...worker:144","...resource_executor:150",
                               "...resource_executor:112","...quotas:84"]}
```

The provider-call quota is charged in `charge_provider_call()` inside the
worker's execution transaction. The API's admission check
(`check_admission`) only counts *active runs*, not the provider-call budget.

So a run that will certainly be rejected still consumes: one API transaction
(advisory lock, two quota counts, three inserts), one outbox row, one dispatcher
poll and AMQP publish with confirm, one worker claim (row lock, attempt row,
lease write), and two audit writes — before the rejection is discovered.

**Mitigating factor, verified:** `RESOURCE_QUOTA_EXCEEDED` is classified
non-retryable (`is_retryable_error()` → `False`, confirmed by 1.0 attempts per
run). There is no retry storm. The waste is one full pipeline traversal per
rejected run, not N.

---

## 12. Memory, GC and Long-Running Behaviour

Sampled across an idle → load → recovery cycle (25 VUs, 40 s):

| Phase | api | dispatcher | worker |
| ----- | --: | ---------: | -----: |
| Idle baseline | 117.3 MiB | 78.26 MiB | 77.88 MiB |
| Load, +15 s | 118.8 MiB | 78.26 MiB | 77.88 MiB |
| Load, +35 s | 118.7 MiB | 78.26 MiB | 77.88 MiB |
| +10 s after load | 118.3 MiB | 78.27 MiB | 77.88 MiB |
| +40 s after load | 118.3 MiB | 78.26 MiB | 77.88 MiB |

**No memory risk found.** Peak growth under load is +1.5 MiB (1.3%), and memory
returns to 118.3 MiB afterwards. Background processes are bit-for-bit flat.
This is the expected profile for a request path that does not materialise large
objects, and it matches the code: no `.all()` without a limit, no full-table
loads, keyset pagination throughout.

**Soak caveat.** The longest continuous window measured here is 40 s. A
`soak/soak.js` scenario is provided for 1 h+ runs in a dedicated environment.
Slow leaks over hours are therefore **Not Tested** — but the flat profile and
the absence of unbounded in-process collections make one unlikely. The growth
that *is* certain is on disk, in `security_audit_events` (PERF-005).

---

## 13. Cold vs Warm

| Measure | Value |
| ------- | ----: |
| Container restart → `/healthz` 200 | 1.95 s |
| First authenticated request | 99.2 ms |
| Third authenticated request | 73.1 ms |
| Warm steady state | 72-81 ms |

Cold-start penalty is ~26 ms on the first request and gone by the third. There
is no JIT warm-up, no lazy-initialisation cliff, and no cache to prime — the
runtime holds no data cache. Startup is dominated by interpreter and import
time, not by connection setup.

Not a problem area. Readiness probe timing in the Helm chart
(`initialDelaySeconds: 5`) comfortably exceeds the 1.95 s observed.

---

## 14. Observability for Performance

The question this section answers: *when latency regresses in production, can
the cause be found?*

| Telemetry | Present? | Notes |
| --------- | -------- | ----- |
| Request rate | Partial | `arr.runs.submitted` counts submissions only, not requests |
| **Request latency** | **No** | — |
| Error rate | Partial | Via `arr.attempts.completed{outcome}` and `arr.security.events{outcome}` |
| **DB latency** | **No** | — |
| **External API latency** | **No** | `arr.provider.calls` counts calls, not duration |
| **Queue depth / lag** | **No** | — |
| CPU / memory / GC | No | Not exported by the application |
| **Connection pool** | **No** | — |
| Distributed tracing | **Yes** | W3C context propagated API → outbox → dispatcher → worker |
| Metric cardinality | **Good** | `safe_provider()` / `safe_error_code()` collapse unknowns to `"other"`; run and attempt IDs explicitly forbidden as labels |

**All 8 metrics are counters. There are zero histograms** — verified by
`grep -c create_histogram` returning 0. No percentile of anything can be
computed from the metrics this system emits.

That is the central observability gap: this audit's core number (read p50 of
87.8 ms, 92% of it authentication) is **invisible to the running system**. It
was obtainable only by attaching a load generator and running a controlled A/B.
Tracing would show the shape on a sampled request, but there is no aggregate
latency signal to alert on or to trend.

The cardinality discipline, by contrast, is genuinely well done and should be
preserved when histograms are added — a latency histogram keyed by `run_id`
would be far worse than no histogram at all.

---

## 15. Findings

### PERF-001 — Per-request PBKDF2 verification dominates all authenticated latency

**Severity:** Critical | **Confidence:** Confirmed | **Category:** Runtime / CPU
**Status:** Confirmed Bottleneck
**Affected Component:** `src/agent_runtime/security/credentials.py:14,33-36`; `src/agent_runtime/api/main.py:285`

**Evidence**

1. Microbenchmark, in-container (linux/arm64, Python 3.14.7, n=12):
   `derive_verifier()` median **61.22 ms** (min 61.08, max 65.11).
   `KDF_ITERATIONS = 600_000`.
2. A/B, identical stack, only `APP_AUTH_MODE` differs:
   `GET /v1/runs/{id}` p50 **87.8 ms → 7.3 ms** (−80.5 ms, 92%).
3. `docker stats` at 71 RPS: API container **849.6% CPU**, PostgreSQL 9.1%.
4. `EXPLAIN ANALYZE` of the query that request runs: **0.028 ms**.

```python
# credentials.py:14,33-36
KDF_ITERATIONS = 600_000


def derive_verifier(raw_key: str, *, salt: str, pepper: str) -> str:
    material = hmac.digest(pepper.encode(), b"arr-credential-v1\0" + raw_key.encode(), "sha256")
    return hashlib.pbkdf2_hmac("sha256", material, bytes.fromhex(salt), KDF_ITERATIONS).hex()
```

**How to reproduce**

```bash
bash performance-tests/scripts/setup-env.sh
set -a; . performance-tests/.env.perf; set +a
docker compose -f docker-compose.yml -f performance-tests/docker-compose.perf.yml up -d
k6 run performance-tests/smoke/smoke.js                      # authenticated

docker compose -f docker-compose.yml -f performance-tests/docker-compose.perf.yml \
  run -d --rm --name arr-api-noauth -p 8001:8000 \
  -e APP_AUTH_MODE=disabled -e APP_ENVIRONMENT=local --no-deps api
ARR_BASE_URL=http://localhost:8001 ARR_API_KEY= k6 run performance-tests/smoke/smoke.js
```

**Observed impact:** read p50 7.3 ms → 87.8 ms (12×). Per-replica ceiling ~71 RPS
at ~8.5 cores instead of a database-bound ceiling orders of magnitude higher.
Amplified by CP-2: `wait_for_terminal` polls every 500 ms, so one submit
triggers ~8 authenticated reads, each paying 61 ms of CPU.

**Root cause:** PBKDF2 is designed to be slow, to make *offline* cracking of a
stolen verifier expensive. That property is correct for storage and wrong for a
per-request hot path. The credential here is a 256-bit random key, not a
human-chosen password — the threat PBKDF2 defends against (low-entropy guessing)
does not apply to it in the first place.

**Recommendation** (in preference order)

1. **Verify high-entropy keys with HMAC, not PBKDF2.** For a
   CSPRNG-generated 256-bit key, `hmac.compare_digest(HMAC(pepper, key), stored)`
   is a complete defence: there is nothing to brute-force. Cost drops from 61 ms
   to microseconds. Keep PBKDF2 only if keys can ever be user-chosen.
2. **If PBKDF2 must stay, cache the verification result.** Key the cache on
   `sha256(raw_key)`, store the resolved principal/tenant, bound it (a few
   thousand entries, 60 s TTL). Each caller then pays 61 ms once per TTL instead
   of per request. Note the trade-off: a revoked credential stays valid for up
   to one TTL, so pair it with a revocation check on the cached path.
3. **Or issue a short-lived bearer token at a dedicated endpoint.** Pay the KDF
   once at token issue; verify the token per request with HMAC. Highest effort,
   also the most conventional design.

Do **not** simply lower the iteration count — that weakens SEC-010 without
addressing the structure. Options 1 and 3 keep the security property and remove
the cost.

**Expected benefit:** authenticated read p50 ≈ 88 ms → ≈ 10 ms. Per-replica
capacity ~71 RPS → bounded by the next constraint (pool/DB), plausibly 5-10×.
CPU per request ~120 ms → <10 ms, which changes the cost of running the service.

**Effort:** Low (option 1/2) / Medium (option 3) | **Risk:** Medium — touches
the authentication path; requires the SEC-E1 test suite to pass unchanged
| **Priority:** **P0**

#### ✅ Resolved — option 1 implemented

`hmac-sha256-v1` is now the default verification scheme.
`pbkdf2-sha256-v1` remains verifiable, so existing registries keep working and
credentials rotate one at a time.

**Verifier cost, same host, same benchmark:**

| Scheme | Median | Single-core ceiling |
| ------ | -----: | ------------------: |
| `pbkdf2-sha256-v1` | 91.263 ms | 11 auth/s |
| `hmac-sha256-v1` | **0.0011 ms** | ~923,000 auth/s |

**End-to-end, authenticated `GET /v1/runs/{id}`**, clean volumes, identical
scenario, 2 repeats × 30 s per level:

| VUs | RPS before | RPS after | p50 before | p50 after | p95 before | p95 after |
| --: | ---------: | --------: | ---------: | --------: | ---------: | --------: |
| 1 | 14.6 | **424** | 66.0 ms | **2.2 ms** | 72.8 ms | 2.8 ms |
| 5 | 52.2 | **~500** | 93.7 ms | 9.5 ms | 111.5 ms | 13.8 ms |
| 10 | 71.0 | ~178 | 132.8 ms | 52.5 ms | 186.5 ms | 106.2 ms |
| 25 | 61.3 | ~320 | 399.4 ms | 73.6 ms | 688.2 ms | 119.8 ms |
| 50 | 56.1 | ~380 | 839.4 ms | 121.0 ms | 1,467.9 ms | 235.5 ms |

Error rate 0.0% at every level. Single-VU latency improved **30×**; peak
throughput improved roughly **7×**.

**The bottleneck moved, as §22 predicted it would.** Two consequences were
measured rather than assumed:

1. **The rate limiter became binding first.** The initial post-fix sweep showed
   24-100% errors: with the KDF gone, traffic exceeded
   `APP_RATE_LIMIT_REQUESTS=10000 / 60 s` (~166 RPS) and the API correctly
   returned `429`. Redis showed the counter at 9,651/10,000. This is PERF-008
   behaving exactly as designed — the numbers above were re-measured with
   `ARR_PERF_RATE_WINDOW=1` so the sweep measures service time, not admission.
2. **The new ceiling is one CPU core, not the connection pool.** Under load the
   API container sits at **~103% CPU** — a single saturated core — where it
   previously sat at ~850% (the KDF parallelised across the threadpool). Meanwhile
   PostgreSQL is at ~32%, Redis at ~1%, and only 1-2 of 11-12 pooled connections
   are active. k6 itself used ~10% CPU, so the generator is not the constraint.

   PERF-009 predicted the pool would bind next. **That prediction was wrong**:
   the pool is nowhere near exhausted. The constraint is that a single uvicorn
   process runs one asyncio event loop on one core. Scaling past ~500 RPS per
   replica needs more processes or more replicas, not a larger pool.

   The reproducible dip at 10 VUs (178 RPS across two runs, against ~500 at 5 VUs
   and ~320 at 25) is **unexplained**. The system is single-core saturated across
   10-50 VUs, so the variation reflects event-loop and host scheduling rather
   than a capacity change — but that is an observation, not a verified mechanism,
   and it is left open.

---

### PERF-002 — Throughput degrades beyond the concurrency knee

**Severity:** High | **Confidence:** Confirmed | **Category:** Concurrency
**Status:** Confirmed Bottleneck
**Affected Component:** API process; anyio threadpool sizing

**Evidence:** read sweep — 71.0 RPS @ 10 VUs → 61.3 @ 25 → **56.1 @ 50**, a 21%
throughput *loss* while p95 rises 186 ms → 1,468 ms (7.9×). Error rate 0.0%
throughout. 15 threads observed on the uvicorn process; anyio's default limiter
allows 40.

**Root cause:** With a ~61 ms CPU-bound task per request and ~10 cores, the
useful concurrency limit is roughly the core count. Beyond it, additional
threadpool workers contend for the same cores; the extra context switching and
cache pressure cost more than the parallelism gains. Classic oversubscription,
made visible because the per-request task is unusually long.

**Recommendation:** Primarily a consequence of PERF-001 — fixing that shrinks
the CPU task and moves the knee far out. Independently: bound the threadpool
explicitly to roughly the CPU quota (`anyio.to_thread.current_default_thread_limiter().total_tokens`)
rather than accepting the default 40, so queueing happens in an ordered way
instead of through core thrashing. Then admission-control at the edge rather
than letting every arriving request compete.

**Expected benefit:** graceful plateau instead of retrograde collapse; p99
bounded under overload.

**Effort:** Low | **Risk:** Low | **Priority:** **P1**

---

### PERF-003 — Every read request performs two database writes

**Severity:** High | **Confidence:** Confirmed | **Category:** Database
**Status:** Confirmed Bottleneck
**Affected Component:** `src/agent_runtime/security/audit.py:34-50`; audit calls in `api/main.py`

**Evidence:** 20 authenticated `GET /v1/runs/{id}` produced exactly **40** new
rows in `security_audit_events` (2.00 per request) — one `AUTHENTICATION/ALLOWED`
record, one sensitive-read record. `SqlAlchemySecurityAuditSink.record()` opens a
**new session and a new transaction per record**:

```python
async def record(self, record: SecurityAuditRecord) -> None:
    async with self._session_factory() as session:
        async with session.begin():
            session.add(SecurityAuditEvent(...))
```

Table state after testing: **25,479 rows / 5,568 kB**, against 17 rows in `runs`.

**Root cause:** SEC-007 (durable audit) and SEC-AUD-01 (sensitive-read auditing)
each write independently, synchronously, in separate transactions, on the
request path. Correct for auditability; expensive as a per-read cost, and it
converts a read-scaling problem into a write-scaling one.

**Recommendation**
- **Batch the two records into one transaction.** They are produced within the
  same request and both are append-only; two `session.add()` calls in one
  `session.begin()` halves the transaction count with no semantic change.
- **Then buffer and flush asynchronously** — a bounded in-memory queue drained
  by a background task with a batched `INSERT`. This changes the durability
  guarantee (records can be lost on a hard crash), so it must be a deliberate
  decision reconciled with the SEC-007 fail-closed policy. If the fail-closed
  guarantee must hold for `DENIED` outcomes, buffer only `ALLOWED` ones.
- Consider whether every successful authentication needs its own row, or
  whether read auditing alone (which carries the principal) is sufficient.

**Expected benefit:** removes ~2 transactions per request; directly reduces the
~19 ms non-KDF authenticated overhead and cuts audit-table growth by up to half.

**Effort:** Low (batching) / Medium (async buffer) | **Risk:** Medium — touches
an audit guarantee | **Priority:** **P1**

---

### PERF-004 — Provider-call quota is enforced at execution, not at admission

**Severity:** High | **Confidence:** Confirmed | **Category:** API / Infrastructure
**Status:** Confirmed Bottleneck
**Affected Component:** `infrastructure/database/quotas.py:84` (`charge_provider_call`); `execution/resource_executor.py:112`

**Evidence:** During the sweeps, **1,925 of 2,058 runs (93.5%)** finished
`FAILED` with `error_code = RESOURCE_QUOTA_EXCEEDED`. Event counts show every
one of them was claimed by a worker first:

| Event | Count |
| ----- | ----: |
| `RUN_QUEUED` | 2,058 |
| `RUN_CLAIMED` | 2,058 |
| `RUN_FAILED` | 1,925 |
| `RUN_SUCCEEDED` | 133 |

**Root cause:** `check_admission()` on the API path counts *active runs* only.
The provider-call budget is charged later, inside the worker's execution
transaction. A run over budget therefore traverses the entire durable pipeline —
API transaction with advisory lock and two quota counts, outbox row, dispatcher
poll and confirmed AMQP publish, worker claim with row lock, attempt row and
lease — before being rejected.

**Verified mitigation:** `RESOURCE_QUOTA_EXCEEDED` is **non-retryable**
(`is_retryable_error()` → `False`; 1.0 attempts per run). There is no retry
amplification. The cost is one wasted pipeline traversal per rejected run.

**Recommendation:** Check the provider-call budget in `check_admission()`, under
the advisory lock the API already holds, and return `429` with `Retry-After`
immediately. Keep the execution-time charge as the authoritative accounting —
the admission check is an optimisation, not a replacement, because budget can be
consumed between admission and execution. This turns the most expensive path
through the system into the cheapest.

**Expected benefit:** a quota-exceeded submission costs one rejected HTTP request
instead of a full durable traversal. Under the measured conditions that is 93.5%
of submissions avoiding the entire pipeline. Also improves client experience:
synchronous `429` instead of an asynchronous `FAILED` discovered by polling.

**Effort:** Medium | **Risk:** Low | **Priority:** **P1**

#### ✅ Resolved

`check_admission()` now calls `check_provider_budget()` under the advisory lock
the API already holds. A submission whose budget is provably spent is rejected
with `429 RESOURCE_QUOTA_EXCEEDED` and a `Retry-After` header, before any
durable record exists.

**The admission check is deliberately advisory.** `charge_provider_call()`
remains the authoritative accounting, because budget can be consumed between
admission and execution by runs already in flight, and because a multi-case
regression job can exhaust its budget part-way through. Admission does not
decrement anything — verified by
`test_admission_budget_check_does_not_consume_budget`, which submits three runs
and asserts the counter stays at zero. Charging at both points would bill an
accepted run twice.

Proven against live PostgreSQL and Redis by
`test_exhausted_provider_budget_is_rejected_at_admission_not_at_the_worker`,
which spends the budget, submits one run, and asserts that the counts of
`runs`, `outbox_events` and `run_attempts` are **unchanged**:

| | Before | After |
| - | ------ | ----- |
| HTTP response | `202 Accepted` | `429 RESOURCE_QUOTA_EXCEEDED` + `Retry-After` |
| Durable records created | run + outbox + attempt + lease | **none** |
| Where the caller learns | polling the status URL | the submit response |
| Pipeline traversed | API → outbox → dispatcher → AMQP → worker | API only |

One existing test changed behaviour and was updated rather than worked around:
`test_durable_evaluation_regression_jobs_and_provider_budget` asserted that a
job submitted with an exhausted budget is accepted and *later* reports
`RESOURCE_QUOTA_EXCEEDED`. It now asserts the synchronous rejection. The
execution-time path it used to cover is still covered directly, and under
concurrency, by `test_atomic_provider_quota_races`.

---

### PERF-005 — `security_audit_events` is unbounded and unindexed for its own access patterns

**Severity:** Medium | **Confidence:** Confirmed | **Category:** Database
**Status:** Confirmed Bottleneck
**Affected Component:** `migrations/versions/20260906_06_*`, `20260912_08_*`

**Evidence**

```
-- forensic lookup: who accessed this run?
 Seq Scan on security_audit_events (actual time=0.036..3.426 rows=12694 loops=1)
   Filter: (target_run_id = '...'::uuid)
   Rows Removed by Filter: 12785
   Buffers: shared hit=511
 Limit total: 4.580 ms

-- retention purge candidate scan
 Seq Scan on security_audit_events (Rows Removed by Filter: 25479)
   Filter: (created_at < (now() - '30 days'::interval))
   Buffers: shared hit=511
```

Indexes present: `security_audit_events_pkey`, `ix_security_audit_events_client_id`.
Neither `target_run_id` nor `created_at` is indexed. Table is the largest in the
database (25,479 rows / 5,568 kB vs 17 rows in `runs`) and grows with **read**
traffic (PERF-003).

**Root cause:** Indexes were added for the write path and for `client_id`
filtering, but not for the two queries the table actually exists to serve:
incident forensics by target, and age-based retention.

**Recommendation**
- `CREATE INDEX CONCURRENTLY ix_security_audit_events_target_run_id ON security_audit_events (target_run_id) WHERE target_run_id IS NOT NULL;`
- `CREATE INDEX CONCURRENTLY ix_security_audit_events_created_at ON security_audit_events (created_at);`
- Consider `(target_run_id, created_at DESC)` to serve the forensic query's sort from the index.
- Implement the retention purge that `APP_PAYLOAD_RETENTION_DAYS` implies but
  that nothing currently executes for this table; partitioning by month makes
  purging a `DROP PARTITION` rather than a bulk `DELETE` — relevant because the
  table is append-only by trigger.

**Expected benefit:** forensic queries from O(n) to O(log n) — 4.6 ms → <0.1 ms
at current size, and bounded as the table grows. Retention becomes feasible at all.

**Effort:** Low (indexes) / Medium (partitioning + purge) | **Risk:** Low
| **Priority:** **P2**

---

### PERF-006 — No latency histograms anywhere in the metrics

**Severity:** Medium | **Confidence:** Confirmed | **Category:** Observability
**Status:** Confirmed Bottleneck
**Affected Component:** `src/agent_runtime/observability/metrics.py`

**Evidence:** `grep -c create_histogram src/agent_runtime/observability/metrics.py`
→ **0**. All 8 instruments are counters: `arr.runs.submitted`,
`arr.attempts.started`, `arr.attempts.completed`, `arr.retries.scheduled`,
`arr.provider.fallbacks`, `arr.outbox.dispatches`, `arr.security.events`,
`arr.provider.calls`.

**Root cause:** Instrumentation was built for correctness and security signals
(the SEC-AUD-02 alerting work), not for performance. The deliberate
low-cardinality discipline is right; the missing instrument type is the gap.

**Recommendation:** Add histograms with the same cardinality discipline —
`arr.http.server.duration{route, method, status_class}` (templated route, never
raw URL), `arr.db.operation.duration{operation}`,
`arr.provider.call.duration{provider}`, `arr.outbox.lag` as an observable gauge.
Follow the OpenTelemetry semantic conventions for HTTP server metrics so the
data is portable. Explicitly keep `run_id`, `attempt_id`, `principal_id` and raw
paths out of labels — the existing `safe_provider()` / `safe_error_code()`
pattern is the model.

**Expected benefit:** the central finding of this audit becomes observable in
production instead of requiring a load generator. Prerequisite for the CI
performance gates in §19.

**Effort:** Low | **Risk:** Low | **Priority:** **P2**

---

### PERF-007 — No response compression negotiated

**Severity:** Low | **Confidence:** Confirmed | **Category:** Network
**Status:** Observation
**Affected Component:** API middleware stack

**Evidence:** `curl -H 'Accept-Encoding: gzip, br'` on `GET .../events` returns
`content-length: 1236` and **no `Content-Encoding` header**.

**Assessment:** Currently harmless. Measured payloads are 260-1,236 bytes; below
roughly 1 KB, compression costs more CPU than it saves bytes — and CPU is
precisely this system's scarce resource (PERF-001). It becomes relevant when a
client requests `limit=100` on the event endpoint, which can plausibly reach
tens of kilobytes.

**Recommendation:** Add `GZipMiddleware(minimum_size=1024)` — or terminate
compression at the ingress, which keeps the CPU off the application. Do not
enable it unconditionally for small responses.

**Effort:** Low | **Risk:** Low | **Priority:** **P3**

---

### PERF-008 — Admission-control ceilings cap per-replica capacity

**Severity:** Low | **Confidence:** Confirmed | **Category:** API
**Status:** Observation
**Affected Component:** `src/agent_runtime/settings.py`

**Evidence:** The stack refused to start with the values initially chosen for
the perf overlay:

```
rate_limit_requests        Input should be less than or equal to 10000
principal_provider_calls   Input should be less than or equal to 10000
tenant_provider_calls      Input should be less than or equal to 20000
```

**Assessment:** These bounds are a **security control** from SEC-E2 and are
working as designed. They are recorded here because they are also a capacity
ceiling, and the two concerns need to be reconciled deliberately rather than
discovered during an incident. `rate_limit_requests ≤ 10000` over a
`window_seconds ≥ 1` permits up to 10,000 RPS per principal, so it does not bind
today. `principal_provider_calls ≤ 10000` per window is the one to watch: it
caps a single principal's provider throughput regardless of available capacity,
and is what produced the 93.5% rejection rate in §11.

**Recommendation:** No change on performance grounds alone. Document the
intended production values and confirm the provider-call ceiling matches the
commercial provider budget. Revisit only alongside PERF-004.

**Effort:** Low | **Risk:** Low | **Priority:** **P3**

---

### PERF-009 — Connection pool is small relative to the threadpool

**Severity:** Low | **Confidence:** Medium | **Category:** Database
**Status:** Potential Issue
**Affected Component:** `src/agent_runtime/infrastructure/database/session.py:14`

**Evidence:** `create_async_engine(url, pool_pre_ping=True)` uses SQLAlchemy's
defaults — pool size 5, max overflow 10, so 15 connections. anyio's threadpool
defaults to 40 workers. No pool exhaustion was observed at any tested
concurrency (zero errors, PostgreSQL under 12% CPU).

**Why this is "Potential" and not confirmed:** the KDF cost currently throttles
arrival into the database layer. Requests spend 61 ms in CPU before touching a
connection, so the pool never sees the concurrency the threadpool could
generate. **Remove PERF-001 and the pool becomes the next candidate
constraint** — this is the finding most likely to appear immediately after the
P0 fix lands.

**Recommendation:** Size the pool explicitly and in relation to the threadpool
and to PostgreSQL's `max_connections` across all replicas, rather than relying
on defaults. Re-run the sweep after PERF-001 and confirm where the new knee sits
before choosing numbers.

**Effort:** Low | **Risk:** Low | **Priority:** **P2** (re-evaluate after P0)

#### ✅ Resolved — but for a different reason than predicted

The prediction above was **wrong on its stated mechanism**. After PERF-001 the
pool was measured, not assumed: at 400+ RPS only **1-2 of 11-12** pooled
connections were active and PostgreSQL sat at ~32% CPU. The pool never became
the request-path bottleneck; a single asyncio event loop on one core did.

The finding survives for a different reason — **arithmetic, not contention**.
Each process opens its own pool, and the shipped chart runs four kinds of them:

| | Per-process pool | Processes | Total | vs `max_connections` 100 |
| - | ---: | ---: | ---: | --- |
| Before | 5 + 10 = 15 | 2 api + 2 worker + 1 + 1 = 6 | **90** | No headroom to scale at all |
| After | 5 + 5 = 10 | 4 api + 2 worker + 1 + 1 = 8 | **80** | Autoscaling fits |

So the pool was never too small — it was too large to scale behind. The engine
now takes `db_pool_size` / `db_max_overflow` from settings instead of
SQLAlchemy's defaults, and the ceiling is asserted by
`tests/unit/test_connection_pool.py`.

**This had to land before the API autoscaler.** An HPA on top of the previous
defaults would have exhausted PostgreSQL instead of serving more traffic.

---

### PERF-010 — A single API pod cannot use more than one core

**Severity:** High | **Confidence:** Confirmed | **Category:** Concurrency / Infrastructure
**Status:** Confirmed Bottleneck — **found by measurement after PERF-001, not in the original audit**
**Affected Component:** `src/agent_runtime/processes.py:33` (`uvicorn.run`), Helm `api` resources

**Evidence:** With the KDF removed, the API container settles at **~103% CPU** —
one saturated core — while PostgreSQL is at ~32%, Redis ~1%, and k6 (the
generator) ~10%. Throughput plateaus around 400-500 RPS per pod and no
additional concurrency raises it.

**Root cause:** One uvicorn process runs one asyncio event loop. Before
PERF-001 this was hidden: the KDF was offloaded to the anyio threadpool and
`pbkdf2_hmac` releases the GIL, so the container genuinely used ~850% CPU.
Remove that work and the remaining request handling is ordinary Python on a
single loop, which is single-core by construction.

**Why not `uvicorn --workers N`:** rejected after inspection.
`_configure_process_observability()` runs inside `run_api()` in the parent
process. With `workers > 1`, uvicorn's children import the app factory fresh and
never execute `run_api`, so structured logging and OTel would be silently
unconfigured in every worker. Fixing that means moving observability setup into
the app factory, which then also runs in every test that calls `create_app()`.
Horizontal scaling avoids the problem entirely and is what Kubernetes is for.

**Recommendation (implemented):** scale by replica, and size the pod to what one
replica can actually use.

- `api.resources` set to `requests == limits == 1` CPU. The previous `500m`
  limit throttled the pod to roughly 6% of what the pre-PERF-001 code needed;
  more than `1` cannot be used by a single event loop. Equal requests and
  limits also give Guaranteed QoS and more predictable latency.
- An API `HorizontalPodAutoscaler` mirroring the existing worker HPA, **disabled
  by default**, with `maxReplicas: 4` chosen to fit the connection ceiling above
  and a 300 s scale-down stabilisation window so pools drain.

**Expected benefit:** capacity becomes `replicas × ~450 RPS` instead of a fixed
~450 RPS. Removes the guaranteed CPU throttling that the old limit caused.

**Effort:** Low | **Risk:** Low | **Priority:** **P1**

> **Operator note.** Enabling *both* autoscalers at their defaults
> (`api.maxReplicas: 4`, `worker.maxReplicas: 10`) gives
> `(4 + 10 + 1 + 1) × 10 = 160` connections, which exceeds PostgreSQL's default
> 100. Raise `max_connections`, lower the pools, or put PgBouncer in front
> before enabling both. This is stated rather than silently defaulted around:
> there is no pool setting that makes both autoscalers safe against a default
> PostgreSQL.

---

## 16. What Was Not Tested

Stated plainly rather than implied.

| Area | Status | Reason |
| ---- | ------ | ------ |
| Frontend / Core Web Vitals / bundles | **Not Applicable** | No frontend exists in this repository |
| Soak > 40 s | **Not Tested** | `soak/soak.js` is provided; 1 h+ needs a dedicated environment |
| Multi-replica scaling | **Not Tested** | Single-replica Compose stack |
| Managed PostgreSQL / cloud CPU quota | **Not Tested** | Local containers only |
| Real OpenAI provider latency | **Not Tested** | Deliberately avoided — third-party system, real cost |
| Production / staging environments | **Not Tested** | Per the audit's safety policy |
| GC pause distribution | **Not Tested** | No in-process GC instrumentation; total memory was flat, so low priority |
| Network latency / TLS overhead | **Not Tested** | Loopback only |
| k6 `stress` / `spike` / `soak` full runs | **Scripts provided, not executed to completion** | The capacity curve was obtained from the controlled sweeps, which give cleaner attribution |

**Correlation vs causation.** PERF-001 is the only finding where causation is
established, via the controlled A/B (one variable changed, 92% of latency
disappeared). PERF-002's *mechanism* (oversubscription) is inferred from the
retrograde curve, the measured CPU-bound task and the observed thread count —
strong, but inferential rather than directly instrumented.

---

## 17. Performance Scorecard

| Category | Score | Notes |
| -------- | ----: | ----- |
| API | 3/10 | Correct and error-free, but every endpoint carries a 61-90 ms fixed cost |
| Database | 8/10 | Hot path 0.028 ms, no N+1, keyset pagination, good indexes — minus the audit table |
| CPU efficiency | 2/10 | ~120 ms CPU for a 0.028 ms query; 8.5 cores for 71 RPS |
| Memory efficiency | 9/10 | +1.5 MiB under load, full recovery, flat background processes |
| Concurrency | 4/10 | Correct threadpool offload; retrograde throughput past the knee |
| Caching | N/A | *No data cache exists.* Redis serves rate limiting only — see note below |
| Frontend | N/A | No frontend |
| Network | 8/10 | Sub-1.3 KB payloads, no chattiness; compression absent but currently irrelevant |
| Infrastructure | 6/10 | Resource limits and probes set; `limits.cpu: 500m` is far below what one replica needs |
| Observability | 4/10 | Excellent tracing and cardinality discipline; zero latency instruments |
| Scalability | 5/10 | Stateless and horizontally scalable, but at ~8.5 cores per 71 RPS |

**On caching:** scored N/A rather than 0 deliberately. The absence of a data
cache is a reasonable design choice for a durability-focused runtime where
reads are by primary key and cost 0.028 ms — caching that would add invalidation
complexity for no measurable gain. The cacheable thing here is the *credential
verification*, which is PERF-001 option 2, not the data.

---

## 18. Top Bottlenecks

| Rank | Finding | Severity | Evidence | Impact | Effort |
| ---: | ------- | -------- | -------- | ------ | ------ |
| 1 | PERF-001 per-request PBKDF2 | Critical | A/B 87.8→7.3 ms; 61.2 ms microbench; 850% CPU | 92% of read latency; caps capacity | Low-Med |
| 2 | PERF-004 quota enforced at execution | High | 1,925/2,058 runs rejected at worker | 93.5% of submissions waste the full pipeline | Med |
| 3 | PERF-003 two DB writes per read | High | 20 GETs → 40 audit rows | 2 extra transactions/request | Low |
| 4 | PERF-002 retrograde throughput | High | 71→56 RPS, p95 186→1468 ms | Overload degrades, not plateaus | Low |
| 5 | PERF-006 no latency histograms | Medium | 0 histograms, 8 counters | Cannot detect any of this in production | Low |
| 6 | PERF-005 audit table unindexed/unbounded | Medium | Seq scan 4.58 ms/25k rows | Forensics and retention degrade linearly | Low-Med |
| 7 | PERF-009 default connection pool | Low | Pool 15 vs threadpool 40 | Next constraint after #1 | Low |
| 8 | PERF-008 admission ceilings | Low | Settings validation errors | Caps per-principal throughput | Low |
| 9 | PERF-007 no compression | Low | No `Content-Encoding` | Only matters at `limit=100` | Low |

---

## 19. Quick Wins

Genuinely low-effort changes with measurable user impact. Micro-optimisations
are deliberately excluded.

| # | Change | Expected effect | Effort |
| - | ------ | --------------- | ------ |
| 1 | **Verify credentials with HMAC instead of PBKDF2** (PERF-001 option 1) | Read p50 ≈88 ms → ≈10 ms. The single highest-impact change available | ~1 h |
| 2 | **Batch the two audit records into one transaction** (PERF-003) | Halves audit transactions per request | ~1 h |
| 3 | **Two indexes on `security_audit_events`** (PERF-005) | Forensic query 4.6 ms → <0.1 ms, bounded as the table grows | ~30 min |
| 4 | **Add an HTTP duration histogram** (PERF-006) | Makes every number in this report observable in production | ~2 h |
| 5 | **Pin the anyio threadpool to the CPU quota** (PERF-002) | Plateau instead of retrograde collapse under overload | ~30 min |
| 6 | **Raise `api.resources.limits.cpu` in the Helm chart** | Currently `500m`; measured need is ~8.5 cores at 71 RPS. Today the chart guarantees CPU throttling under any real load | ~15 min |

Item 6 deserves emphasis: the chart's `limits.cpu: 500m` against a measured
~850% requirement means a deployed replica would be throttled to roughly 6% of
the CPU it needs. That is a configuration mismatch rather than a code defect,
but it would dominate production behaviour — and **CPU throttling would look
exactly like application slowness**, which is the classic misdiagnosis this kind
of audit exists to prevent. Fixing PERF-001 is what makes `500m` realistic;
until then the limit and the workload disagree by more than an order of magnitude.

---

## 20. Scalability Assessment — 10× traffic

**Assumptions stated explicitly:** current load is taken as the measured
single-replica knee (~71 RPS reads / ~53 RPS submits); 10× means ~710 RPS reads;
request mix stays CP-2-dominant; PostgreSQL remains a single primary.

| Dimension | Assessment |
| --------- | ---------- |
| Statelessness | **Ready.** No session state, no local files, no sticky routing. Identity comes from the credential registry; quotas and idempotency are PostgreSQL-authoritative |
| Horizontal scaling | **Works, expensively.** ~710 RPS needs ~10 replicas at ~8.5 cores each ≈ 85 cores, purely for credential verification. With PERF-001 fixed the same load needs a small fraction of that |
| Database capacity | **Ready for reads.** Hot path is 0.028 ms / 2 buffers; 710 RPS is ~20 ms/s of query time. Writes are the concern: 710 reads/s × 2 audit inserts = 1,420 writes/s on an append-only unpartitioned table |
| Shared cache | **Ready.** Redis holds only rate-limit counters; ~2% CPU at current load |
| Local state | **Ready.** Heartbeat files are per-pod and ephemeral |
| Queue | **Ready.** Outbox backlog stayed at 0 of 10,737; dispatcher keeps up |
| Third-party limits | **Insufficient evidence.** OpenAI rate limits and latency were deliberately not tested |
| Connection pool | **Not ready.** 15 connections/replica × 10 replicas = 150 against PostgreSQL's default `max_connections` of 100. This would fail |

### Verdict: **Partially Ready**

Scaling out works architecturally — the design is genuinely stateless and the
durability model is sound. Two things must be settled first:

1. **PERF-001**, or 10× traffic means ~85 cores for authentication alone.
2. **Connection-pool arithmetic** (PERF-009), which breaks at 10 replicas
   against default PostgreSQL settings before anything else does.

PERF-003's write amplification (1,420 writes/s at 10×) and PERF-005's unindexed,
unpartitioned audit table become the following constraint.

---

## 21. Optimization Roadmap

### P0 — Immediate

| Problem | Action | Expected benefit | Effort |
| ------- | ------ | ---------------- | ------ |
| PERF-001 PBKDF2 per request | HMAC verification for high-entropy keys, or bounded verification cache | Read p50 88→~10 ms; capacity 5-10×; CPU/request 120→<10 ms | Low-Med |

### P1 — High impact

| Problem | Action | Expected benefit | Effort |
| ------- | ------ | ---------------- | ------ |
| PERF-004 quota checked too late | Check provider-call budget in `check_admission()`; keep execution-time charge authoritative | 93.5% of rejections avoid the full pipeline; sync `429` instead of async `FAILED` | Med |
| PERF-003 two writes per read | Batch both audit records into one transaction; then evaluate async buffering | −2 transactions/request; halves audit growth | Low-Med |
| PERF-002 retrograde throughput | Bound the threadpool to the CPU quota; admission-control at the edge | Plateau instead of collapse; bounded p99 | Low |
| Helm `limits.cpu: 500m` | Align with measured need, or land PERF-001 first | Prevents guaranteed CPU throttling in production | Low |

### P2 — Scalability

| Problem | Action | Expected benefit | Effort |
| ------- | ------ | ---------------- | ------ |
| PERF-009 default pool | Size pool explicitly vs threadpool and `max_connections`; re-measure after P0 | Removes the next ceiling before it is hit | Low |
| PERF-006 no histograms | HTTP / DB / provider duration histograms, cardinality-safe | Production visibility; enables CI gates | Low |
| PERF-005 audit table | Two indexes now; partition + purge before volume grows | Bounded forensics and retention | Low-Med |

### P3 — Optimization / hardening

| Problem | Action | Expected benefit | Effort |
| ------- | ------ | ---------------- | ------ |
| PERF-007 no compression | `GZipMiddleware(minimum_size=1024)` or ingress-level | Bandwidth on large event pages | Low |
| PERF-008 admission ceilings | Document intended production values; reconcile with provider budget | Predictable capacity | Low |
| Soak coverage | Run `soak/soak.js` for 1 h+ in a dedicated environment | Confirms the absence of slow leaks | Low |

---

## 22. Before / After Benchmark Methodology

No optimisation should be accepted without a measurement produced the same way
as its baseline. Two numbers are comparable only when **all** of the following
match:

| Dimension | Must be identical |
| --------- | ----------------- |
| Dataset | Same table sizes. Audit and event tables are append-only, so reset with `docker compose down -v` between campaigns — otherwise growth alone shifts results |
| Environment | Same host, same container CPU/memory allocation, same PostgreSQL/Redis/RabbitMQ versions |
| Configuration | Same `APP_*` values, especially `APP_AUTH_MODE` and every quota/limit |
| Warm-up | Discard the first ~10 s; cold start costs ~26 ms on the first request |
| Concurrency | Same VU count and executor |
| Duration | Same window (20 s for sweeps, 60 s for load) |
| Scenario | Same script, unmodified |
| Generator | k6 below saturation — verify with `docker stats` |

**Procedure**

```bash
# 1. BEFORE - clean state
docker compose -f docker-compose.yml -f performance-tests/docker-compose.perf.yml down -v
bash performance-tests/scripts/setup-env.sh
set -a; . performance-tests/.env.perf; set +a
docker compose -f docker-compose.yml -f performance-tests/docker-compose.perf.yml up -d --build

RUN_ID=$(curl -s -X POST http://localhost:8000/v1/runs -H 'Content-Type: application/json' \
  -H "X-API-Key: $ARR_API_KEY" -H "Idempotency-Key: base-$(date +%s)" \
  --data '{"input":{"prompt":"seed"}}' | sed -nE 's/.*"run_id":"([^"]+)".*/\1/p')

for vus in 1 5 10 25 50; do
  ARR_VUS=$vus ARR_DURATION=20s ARR_RUN_ID=$RUN_ID \
    k6 run --quiet --summary-export=performance-tests/results/before-read-$vus.json \
    performance-tests/scripts/read-sweep.js
done

# 2. Apply exactly one change.

# 3. AFTER - repeat step 1 verbatim, writing after-read-$vus.json.

# 4. Compare p50/p95/RPS per VU level, and CPU from docker stats.
```

**Report both the improvement and the new knee.** A change that improves p50 but
moves saturation to a lower VU count is not an improvement. For PERF-001
specifically, expect the knee to move outward and the next constraint (likely
the connection pool) to appear — that is a successful outcome, not a regression.

---

## 23. Performance Quality Gates for CI

This repository has **no declared SLO**, so this section proposes gates relative
to a measured baseline rather than inventing production targets.

**Prerequisite:** PERF-006. Without latency histograms there is nothing to gate
on outside a load test.

### Stage 1 — available today

Add to the existing `quality` job:

```yaml
- name: Performance smoke
  run: |
    bash performance-tests/scripts/setup-env.sh
    set -a; . performance-tests/.env.perf; set +a
    docker compose -f docker-compose.yml \
                   -f performance-tests/docker-compose.perf.yml up -d --build
    k6 run performance-tests/smoke/smoke.js     # thresholds already enforced
```

`smoke.js` already gates on `checks rate==1.00` and `http_req_failed rate==0.00`.
This catches functional breakage in the performance path at negligible cost.

### Stage 2 — after a baseline is committed

Record `performance-tests/results/baseline.json` from a controlled run, then gate
on **relative regression** rather than absolute numbers:

| Gate | Threshold | Rationale |
| ---- | --------- | --------- |
| `arr_read_latency` p95 | > baseline + 25% | Catches regressions without encoding a hardware-specific number |
| `arr_submit_latency` p95 | > baseline + 25% | As above, write path |
| Peak RPS at 10 VUs | < baseline − 15% | Throughput regressions do not always show as latency |
| `http_req_failed` | > 0.1% | Errors under normal load |
| CPU per request | > baseline + 30% | Catches an efficiency regression even when latency holds |

Shared CI runners are noisy; use a ±25% band and require two consecutive
failures before blocking, or run the gate nightly rather than per-PR.

### Stage 3 — production SLOs

Once histograms exist and real traffic is observed, replace the synthetic
baseline with percentiles from production and agree actual SLOs with the service
owner. **Do not invent them from this report** — these numbers come from one
laptop.

---

## 24. Conclusions

**1. What is the biggest performance bottleneck today?**
Per-request PBKDF2 credential verification, 600,000 iterations, 61 ms of CPU on
every authenticated request (PERF-001).

**2. What evidence supports this?**
Three independent measurements agreeing: an in-container microbenchmark of
`derive_verifier()` at 61.22 ms median; a controlled A/B where disabling
authentication alone moved read p50 from 87.8 ms to 7.3 ms (−92%); and
`docker stats` showing 849.6% CPU in the API container while PostgreSQL sat at
9.1% and the query itself measured 0.028 ms under `EXPLAIN ANALYZE`.

**3. At what load does the system begin to degrade?**
Throughput peaks at **71 RPS at 10 concurrent users** and then *falls* — 61.3 RPS
at 25 VUs, 56.1 at 50 — while p95 rises from 186 ms to 1,468 ms. Submits peak at
53.5 RPS. Error rate stays 0.0%: the system degrades by slowing down, not by
failing.

**4. Which three problems should be fixed first?**
PERF-001 (per-request KDF), PERF-004 (quota enforced after the full pipeline has
run), PERF-003 (two database writes per read request).

**5. What is the biggest database problem?**
Not the query path — that is excellent (0.028 ms, two buffers, no N+1, keyset
pagination). It is `security_audit_events`: unbounded, growing with *read*
traffic at two rows per request, and lacking indexes on the two columns its own
queries use (`target_run_id`, `created_at`), both of which currently sequential-scan.

**6. What is the biggest memory/CPU problem?**
CPU, decisively — ~120 ms per request for 0.028 ms of database work, 8.5 cores
for 71 RPS. Memory is a non-issue: +1.5 MiB under load with full recovery and
no leak indication.

**7. What is the biggest frontend problem?**
**Not applicable.** This repository contains no frontend — it is a JSON API with
a Python SDK. No bundle, no browser surface, no Core Web Vitals to measure.

**8. Is the system ready for 10× traffic?**
**Partially Ready.** The architecture is genuinely stateless and scales out, but
10× would require roughly 85 cores for authentication alone, and 10 replicas ×
15 pooled connections would exceed PostgreSQL's default `max_connections` of 100
before any other limit is reached. Fix PERF-001 and PERF-009 and the answer
becomes Ready for the measured workload.

**9. Which optimizations give the highest measurable impact?**
Replacing PBKDF2 with HMAC verification for high-entropy keys: roughly one hour
of work, ~88 ms → ~10 ms on the highest-volume endpoint, and a 5-10× capacity
increase. Nothing else in this report is within an order of magnitude of it.

**10. Which performance tests should be added to CI?**
Immediately: `smoke.js`, which already enforces correctness thresholds and costs
little. After PERF-006 lands: a baseline-relative gate on `arr_read_latency` p95,
`arr_submit_latency` p95, peak RPS, error rate and CPU-per-request — as relative
regressions against a committed baseline, not as invented absolute SLOs.

---

## 25. Closing Note on the Security/Performance Trade-off

The dominant finding of this audit was introduced by the security remediation in
`SECURITY_AUDIT.md`. SEC-010 correctly identified an unsalted, single-round
SHA-256 credential verifier and replaced it with a properly peppered, salted,
600,000-iteration PBKDF2. That fixed a real weakness.

The cost appeared because the strong KDF was applied on **every request** rather
than once per session. PBKDF2's slowness is a feature aimed at offline attack on
a stolen verifier; paying it repeatedly online defends against nothing extra.

This is worth stating plainly for two reasons. First, the fix does not require
weakening SEC-010: an HMAC verification of a 256-bit CSPRNG key offers no
brute-force surface to begin with, and a short-lived token pays the KDF once.
Second, it is a reminder that a control's *placement* is as consequential as its
strength — and that neither a security audit nor a performance audit finds this
class of problem alone.

---

## 26. References

Consulted for behaviour that measurement alone could not settle. Blog posts and
Q&A sites were not used as primary technical evidence.

**Framework / runtime**
- FastAPI — Concurrency and `async`/`await`: https://fastapi.tiangolo.com/async/
- Starlette — Middleware and `run_in_threadpool`: https://www.starlette.io/middleware/
- AnyIO — Worker threads and the default thread limiter: https://anyio.readthedocs.io/en/stable/threads.html
- Uvicorn — Deployment and worker model: https://www.uvicorn.org/deployment/
- Python — `hashlib.pbkdf2_hmac` and GIL release in C implementations: https://docs.python.org/3/library/hashlib.html
- Python — `hmac.compare_digest`: https://docs.python.org/3/library/hmac.html

**Database**
- PostgreSQL 17 — `EXPLAIN`: https://www.postgresql.org/docs/17/sql-explain.html
- PostgreSQL 17 — Using EXPLAIN, plan node interpretation: https://www.postgresql.org/docs/17/using-explain.html
- PostgreSQL 17 — `max_connections` and connection resources: https://www.postgresql.org/docs/17/runtime-config-connection.html
- PostgreSQL 17 — Table partitioning: https://www.postgresql.org/docs/17/ddl-partitioning.html
- PostgreSQL 17 — Monitoring statistics (`pg_stat_database`, `pg_stat_user_tables`): https://www.postgresql.org/docs/17/monitoring-stats.html
- SQLAlchemy 2.0 — Connection pooling: https://docs.sqlalchemy.org/en/20/core/pooling.html
- SQLAlchemy 2.0 — asyncio extension: https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html

**Tooling**
- k6 — Test types (smoke, average, stress, spike, soak): https://grafana.com/docs/k6/latest/testing-guides/test-types/
- k6 — Thresholds: https://grafana.com/docs/k6/latest/using-k6/thresholds/
- k6 — Scenarios and executors: https://grafana.com/docs/k6/latest/using-k6/scenarios/

**Standards**
- OpenTelemetry — Metrics semantic conventions for HTTP servers: https://opentelemetry.io/docs/specs/semconv/http/http-metrics/
- OpenTelemetry — Metrics data model: https://opentelemetry.io/docs/specs/otel/metrics/data-model/
- W3C — Trace Context: https://www.w3.org/TR/trace-context/

**Container / orchestration**
- Kubernetes — CPU limits and throttling behaviour: https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/
- Docker — `docker stats` and resource reporting: https://docs.docker.com/reference/cli/docker/container/stats/

---

*Measurements taken 2026-09-12 on the environment in §2. No application code was
modified. No production or third-party system was contacted. All load was
generated against a disposable local stack bound to `127.0.0.1`.*
