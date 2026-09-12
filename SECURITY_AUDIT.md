# Security Audit Report

**Target:** Agent Reliability Runtime (`agent-reliability-runtime` v0.1.0)
**Revision audited:** `27b1d16` (branch `main`, clean working tree)
**Audit date:** 2026-09-12
**Audit type:** Authorized white-box application security review (source, configuration, infrastructure-as-code, CI/CD, supply chain)
**Auditor role:** Senior Application Security Engineer

---

> ## ⚠️ Remediation status — read this first
>
> **This report describes commit `27b1d16`. Every finding in it has since been
> remediated on `main`.** The findings below are written in the present tense
> because that is how they were true at the time of the audit; they do not
> describe the current state of this repository.
>
> | Epic | Findings | Commit |
> | ---- | -------- | ------ |
> | SEC-E1 — Identity, authentication, tenant authorization | SEC-001, 006, 008, 009, 010, 017 | `507305a` |
> | SEC-E2 — API resource controls and safe execution | SEC-002, 003, 004, 005, 020 | `9f67712` |
> | SEC-E3 — Auditability, telemetry, data protection | SEC-007, 013, 014, 022 | `44cec13` |
> | SEC-E4 — Kubernetes, secrets, data-plane least privilege | SEC-011, 012, 018, 019 | `b3914b5` |
> | SEC-E5 — Secure supply chain and delivery | SEC-015, 016, 023 | `9b022dd` |
> | SEC-E6 — Local development surface | SEC-021 | `ca15ba7` |
>
> Two corrections to the report itself, both documented in place:
>
> - **SEC-SC-02 was a false positive.** The Terraform provider binaries were
>   never committed — see §6 of [`docs/security/supply-chain.md`](docs/security/supply-chain.md).
> - **SEC-022 is partially remediated.** Input minimization and the
>   encryption/crypto-shredding design and interface are in place; binding it to
>   a production key-management service is a deliberate deferral.
>
> The report is published as a record of the review and its method, not as a
> live vulnerability disclosure. If you are running a build at or before
> `27b1d16`, the findings apply to you — upgrade.

---

## 1. Executive Summary

The Agent Reliability Runtime is a well-engineered piece of reliability infrastructure. Its durability
story — transactional outbox, worker leases, optimistic concurrency, append-only event triggers,
bounded retry with dead-lettering — is genuinely above the quality bar for a V0, and several security
controls (log allowlisting, metric cardinality control, provider-error sanitization, constant-time
credential comparison, secret-free Git tree) are implemented deliberately and correctly.

The security weaknesses are **not** the ones a scanner finds. Semgrep ran 310 rules across the
repository and returned **zero findings**; the entire locked dependency tree has **one** advisory, and
it is dev-only. There is no SQL injection, no command injection, no unsafe deserialization, no
path traversal, and no user-controlled outbound URL anywhere in the codebase.

The real risk concentrates in three design decisions:

1. **Authorization has no server-side anchor.** Tenant scope, idempotency scope, and rate-limit
   scope are all derived from `X-Client-Id`, a header the caller sets freely. There is exactly one
   shared API key for the entire deployment, so any credential holder can read, evaluate and replay
   any other tenant's runs simply by changing a header.
2. **The API accepts caller-supplied executable policy.** Evaluation rules carry caller-written
   regular expressions and JSON Schemas that run synchronously on the API event loop with no timeout,
   and the run policy snapshot accepts caller-chosen retry bounds that override every server-side
   limit in `Settings`.
3. **Resource consumption is effectively unmetered.** The most expensive endpoint in the system,
   `POST /v1/evaluation-regressions`, requires no client identifier at all and therefore never
   touches the rate limiter, while driving up to 200 provider calls per request.

Every finding below was verified against the running code in-process. Findings that could not be
confirmed are reported as false positives in §25, not as findings.

### Finding counts

| Severity | Count |
| -------- | ----- |
| Critical | 0 |
| High | 4 |
| Medium | 8 |
| Low | 9 |
| Informational | 2 |
| **Total** | **23** |

### Overall security posture

**Needs Improvement.**

This is not a poorly built system — it is a system whose reliability engineering has outpaced its
authorization engineering. The transport and persistence layers are sound; the trust model on top of
them is not yet finished. None of the four High findings requires an exotic precondition: each is
reachable by a legitimate integrating client with the credential it was issued. The remediation work
is well-bounded (see §25 roadmap) and does not require re-architecting the runtime.

---

## 2. Scope

### In scope and reviewed

| Area | Artifacts |
| ---- | --------- |
| Application source | `src/agent_runtime/**` — 26 Python modules, 100% read manually |
| HTTP API | `api/main.py`, `api/schemas.py` — all 9 routes |
| AuthN / AuthZ | `security/authentication.py`, `security/audit.py`, the `enforce_security_boundary` middleware |
| Domain logic | `domain/retry.py`, `domain/routing.py`, `domain/states.py` |
| Persistence | `infrastructure/database/**`, `migrations/versions/*` (6 migrations) |
| Messaging | `infrastructure/messaging/**` (publisher, dispatcher, worker) |
| Providers | `providers/openai_responses.py`, `providers/deterministic.py`, `providers/registry.py` |
| Evaluation | `evaluation/engine.py`, `evaluation/regression.py`, `evaluation/cli.py` |
| Client SDK | `client.py`, `examples/recruiter_demo.py` |
| Container | `docker/Dockerfile`, `docker-compose.yml`, `scripts/compose-smoke.sh` |
| Kubernetes | `charts/agent-reliability-runtime/**`, `deploy/kubernetes/local-dependencies.yaml` |
| IaC | `infra/terraform/**` (3 environments, 1 module) |
| CI/CD | `.github/workflows/ci.yml` |
| Supply chain | `pyproject.toml`, `uv.lock` (78 packages) |
| Configuration | `.env.example`, `alembic.ini`, `Makefile`, `.gitignore` |
| Observability | `observability/{logging,metrics,telemetry}.py`, `docker/otel-collector.yaml`, Prometheus/Grafana provisioning |
| Git history | All 21 commits scanned for credential patterns |
| Tests | All 128 tests read, to establish which controls have regression coverage |

### Explicitly out of scope

- Production deployments, cloud accounts, cluster RBAC, network firewalls, WAF/ingress configuration
- The real values inside the pre-provisioned Kubernetes Secret
- Live OpenAI API behaviour and OpenAI's own security posture
- Grafana/Prometheus/RabbitMQ/PostgreSQL upstream product vulnerabilities
- Penetration testing against any deployed instance

### Testing constraints honoured

No destructive testing, no DoS/stress testing, no third-party system testing, no production access,
and no real credentials were used. All dynamic verification ran **in-process** against
`create_app()` with stub services — no network sockets, no database, no message broker. Source code
was not modified.

---

## 3. Methodology

### Standards applied

- OWASP Application Security Verification Standard (ASVS) 5.0.0
- OWASP Top 10:2025
- OWASP API Security Top 10:2023
- OWASP Web Security Testing Guide (WSTG) — input handling, session, business logic chapters
- OWASP Secure Code Review guidance
- OWASP Software Component Verification Standard (SCVS) — supply chain posture
- CWE Top 25 (2025)
- NIST SSDF (PW/PS practice families)
- SLSA v1.2 — build provenance maturity
- FIRST CVSS v4.0 — vectors provided; see the scoring note in §24

### Review techniques

1. **Reconnaissance** — full file inventory, technology-stack identification, ADR/contract review.
2. **Attack surface enumeration** — every route, consumer, CLI entry point, and exposed port.
3. **Trust boundary and data flow tracing** — source-to-sink for every untrusted input.
4. **Threat modelling** — repository-specific assets, actors, and abuse cases.
5. **Automated analysis** — Semgrep (SAST), pip-audit + OSV batch API (SCA), dependency/lockfile
   review, Git history credential scanning.
6. **Manual code review** — 100% of `src/`, all migrations, all IaC, all CI.
7. **Behavioural verification** — 17 in-process experiments confirming or refuting each candidate
   finding against the real code paths.
8. **False-positive elimination** — every candidate was reproduced before being reported.

### Commands executed

```bash
uvx semgrep scan --config p/python --config p/security-audit --config p/secrets \
  --config p/dockerfile --config p/terraform --metrics=off \
  src tests docker charts infra scripts migrations examples .github

uvx --from pip-audit pip-audit --path .venv/lib/python3.13/site-packages

curl -X POST https://api.osv.dev/v1/querybatch -d @<77 locked packages>

git log -p --all | grep -Ei '(api[_-]?key|secret|password|token|BEGIN .*PRIVATE KEY|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,})'

uv run pytest            # 128 passed — baseline confirmed healthy
uv run python verify.py  # 12 in-process security experiments
uv run python verify2.py # 5 further bounded experiments
```

---

## 4. Architecture & Technology Stack

### Stack detected

| Layer | Technology |
| ----- | ---------- |
| Language | Python 3.12+ (strict mypy, Ruff) |
| API framework | FastAPI 0.141 / Starlette / Uvicorn 0.52 |
| Validation | Pydantic 2.13, pydantic-settings 2.15 |
| ORM / migrations | SQLAlchemy 2.0 (async) / Alembic 1.19 |
| Database | PostgreSQL 17 (asyncpg for runtime, psycopg for migrations) |
| Cache / limiter | Redis 7 (`redis-py` 6.4, asyncio) |
| Message broker | RabbitMQ 4 (`aio-pika` 9.6) with publisher confirms |
| Observability | OpenTelemetry 1.44 (OTLP/HTTP), Prometheus, Grafana, JSON logs |
| Provider | OpenAI Responses API via `httpx` 0.28 |
| Container | `python:3.12-slim`, `uv` 0.6.3, non-root user |
| Orchestration | Kubernetes + Helm (chart v0.1.0) |
| IaC | Terraform 1.14 with `hashicorp/kubernetes` ~2.35 |
| CI | GitHub Actions |

### Process topology

Four independent processes share one codebase and one configuration Secret:

```
                         ┌──────────────────────────────────────┐
   client ──HTTP──▶      │  api          (uvicorn, 0.0.0.0:8000)│──┐
                         └──────────────────────────────────────┘  │
                         ┌──────────────────────────────────────┐  │
                         │  dispatcher   (outbox → RabbitMQ)    │──┤
                         └──────────────────────────────────────┘  ├──▶ PostgreSQL
                         ┌──────────────────────────────────────┐  │
                         │  worker       (RabbitMQ → provider)  │──┤
                         └──────────────────────────────────────┘  │
                         ┌──────────────────────────────────────┐  │
                         │  scheduler    (lease + retry recovery)│──┘
                         └──────────────────────────────────────┘
```

### Request lifecycle

```
POST /v1/runs
  → enforce_security_boundary middleware   (size → authN → rate limit → audit)
  → trace_http_request middleware          (accepts caller traceparent)
  → CreateRunRequest validation            (extra="forbid", 64 KiB input cap)
  → build_policy_snapshot()                (merges caller policy with defaults)
  → resolve_routing_decision()             (catalog-owned metrics, not caller metrics)
  → SqlAlchemyRunService.submit()          ── ONE transaction ──▶ Run + RunEvent + OutboxEvent
  → 202 Accepted

OutboxDispatcher  → SELECT … FOR UPDATE SKIP LOCKED → publish (mandatory + confirm) → published_at
RabbitMqWorker    → claim (row lock + lease) → attempt → provider → durable terminal write → ack
Scheduler         → expired leases → RETRY_SCHEDULED; due retries → fresh outbox event
```

### Architectural security observations

- **Correct:** the API never calls a provider on the submission path and never waits on the broker.
  The blast radius of a provider outage is genuinely contained.
- **Correct:** the outbox makes the "accepted but not queued" window impossible, and the worker's
  lease makes duplicate delivery harmless.
- **Weak:** the API process is *not* purely a submission boundary. `POST /v1/evaluation-regressions`
  and `POST /v1/runs/{id}/evaluations` both perform unbounded CPU and (for the former) provider I/O
  inline on the API event loop, which contradicts ADR-0002's separation principle and is the root
  cause of SEC-002 and SEC-003.
- **Weak:** all four processes receive the identical Secret, so the API pod holds the provider API
  key and the migration Job's DDL credential even though neither needs them.

---

## 5. Attack Surface

### HTTP endpoints

| # | Method | Path | Auth required | Tenant scoping | Mutates state | Returns sensitive data |
| - | ------ | ---- | ------------- | -------------- | ------------- | ---------------------- |
| 1 | GET | `/healthz` | **No** | n/a | No | No |
| 2 | GET | `/docs` | **No** | n/a | No | API structure |
| 3 | GET | `/redoc` | **No** | n/a | No | API structure |
| 4 | GET | `/openapi.json` | **No** | n/a | No | Full API schema (8 paths) |
| 5 | POST | `/v1/runs` | Yes¹ | `X-Client-Id` | **Yes** — creates run + outbox | Run ID, routing decision |
| 6 | GET | `/v1/runs/{run_id}` | Yes¹ | `X-Client-Id` | No | Run status, error code, routing |
| 7 | GET | `/v1/runs/{run_id}/attempts` | Yes¹ | `X-Client-Id` | No | Attempt history, providers, latency |
| 8 | GET | `/v1/runs/{run_id}/events` | Yes¹ | `X-Client-Id` | No | **Full event metadata** |
| 9 | POST | `/v1/runs/{run_id}/evaluations` | Yes¹ | `X-Client-Id` | **Yes** — writes evaluation | Rule results |
| 10 | GET | `/v1/runs/{run_id}/evaluations` | Yes¹ | `X-Client-Id` | No | Evaluation history |
| 11 | POST | `/v1/runs/{run_id}/replay` | Yes¹ | `X-Client-Id` | **Yes** — creates new run | Replay lineage |
| 12 | POST | `/v1/evaluation-regressions` | Yes¹ | **None** | No DB write, **yes provider calls** | Full regression report |

¹ Only when `APP_AUTH_MODE=api_key`. The shipped default is `disabled`, in which case **every**
endpoint is anonymous.

### Endpoint classification

- **Anonymous (by design):** `/healthz`
- **Anonymous (unintended):** `/docs`, `/redoc`, `/openapi.json` — see SEC-009
- **Authenticated, tenant-scoped:** endpoints 5–11 — but the scope is caller-asserted (SEC-001)
- **Authenticated, unscoped:** endpoint 12 — no client identity at all (SEC-003)
- **Admin / internal:** *none exist.* There is no privilege tier above "holds the API key."
- **Service-to-service:** none; inter-process communication is via PostgreSQL and RabbitMQ only.

### Caller-controlled identifiers

| Identifier | Source | Server-side validation |
| ---------- | ------ | ---------------------- |
| `run_id` | URL path | UUID format; ownership checked against caller-asserted `client_id` |
| `X-Client-Id` | Header | **Non-empty only.** No length cap, no allowlist, no credential binding |
| `Idempotency-Key` | Header | Regex `^[A-Za-z0-9][A-Za-z0-9._:-]{7,254}$` — well validated |
| `policy.*` | Body | Partial: retry fields type-checked, but **unbounded**; unknown keys retained |
| `input.*` | Body | Size only (64 KiB); structure is free-form JSON |
| `rules[].value` | Body | **None** — becomes a live regex |
| `rules[].schema` | Body | **None** — becomes a live JSON Schema |
| `traceparent` / `tracestate` | Header | None — fed directly to the OTel propagator |

### Non-HTTP surface

| Surface | Exposure |
| ------- | -------- |
| RabbitMQ consumer (`agent_runtime.execution.v2`) | Requires broker credentials. Message body is minimal (`event_id`, `run_id`, `trace_context`); malformed messages are rejected without requeue. **Well designed.** |
| Legacy queue drain (`agent_runtime.execution`) | Same handler, consumed but not bound — intentional migration path. |
| Dead-letter queue | Durable, receives both dead-lettered runs and poison messages. |
| `agent-runtime-regression` CLI | Local operator only; reads a dataset path from argv. |
| Exposed ports (Compose) | 8000 (API), 5432 (PG), 6379 (Redis), 5672/15672 (RabbitMQ), 9090 (Prometheus), 3000 (Grafana), 4318/8889 (OTel) — all bound to the host. Dev-only; see SEC-021. |
| Exposed ports (Helm) | ClusterIP:8000 only. Correct. |

---

## 6. Trust Boundaries & Data Flows

### Trust boundary map

```
  ┌─ TB-1 ─────────────────────────────────────────────────────────────────┐
  │  Internet / calling application                    TRUST LEVEL: NONE   │
  └────────────────────────────┬───────────────────────────────────────────┘
                               │  HTTP + X-API-Key + X-Client-Id
                               │  ⚠ authN proves "a valid key"; it does NOT
                               │    establish WHICH tenant is calling
  ┌─ TB-2 ─────────────────────▼───────────────────────────────────────────┐
  │  API process            TRUST LEVEL: AUTHENTICATED-BUT-UNIDENTIFIED    │
  │  holds: DB creds, Redis creds, RabbitMQ creds, OPENAI KEY (per Helm)   │
  └────────────┬─────────────────────────────────────┬────────────────────┘
               │ SQL (asyncpg)                       │ Redis INCR/EXPIRE
  ┌─ TB-3 ─────▼──────────────┐        ┌─ TB-4 ──────▼────────────────────┐
  │  PostgreSQL               │        │  Redis                           │
  │  app role owns schema     │        │  rate-limit counters only        │
  │  TRUST: HIGH              │        │  TRUST: HIGH (no authN locally)  │
  └─────┬─────────────────────┘        └──────────────────────────────────┘
        │ outbox polling
  ┌─ TB-5▼────────────────────┐
  │  Dispatcher → RabbitMQ    │  TRUST: HIGH — internal network only
  └─────┬─────────────────────┘
  ┌─ TB-6▼────────────────────┐
  │  Worker                   │  executes the run's IMMUTABLE policy snapshot
  │  holds: OPENAI KEY        │  ⚠ that snapshot contains caller-chosen values
  └─────┬─────────────────────┘
  ┌─ TB-7▼────────────────────┐
  │  OpenAI Responses API     │  TRUST: EXTERNAL — response is parsed defensively,
  │  (fixed base_url)         │  error bodies are never persisted. GOOD.
  └───────────────────────────┘
```

### The critical boundary observation

**TB-1 → TB-2 authenticates but does not identify.** A successful credential check yields exactly one
bit of information: "the caller knows the one shared key." Everything downstream that looks like an
authorization decision — `WHERE client_id = :client_id`, the Redis limiter bucket, the idempotency
uniqueness constraint — consumes an identity the *caller* supplied across that boundary. The data
never crosses from untrusted to trusted; it is merely relabelled.

### Sensitive data flows

| Data | Path | Protection | Assessment |
| ---- | ---- | ---------- | ---------- |
| Raw API key | Client → API header → `hashlib.sha256()` → discarded | Never stored, never logged, never traced | **Good** |
| API key digest | `Settings` → `hmac.compare_digest` | Constant-time comparison | **Good** |
| Credential fingerprint | authN → `security_audit_events.credential_fingerprint` | Persisted | **Weak** — identical to the configured verifier (SEC-010) |
| OpenAI API key | Secret → worker env → `Authorization: Bearer` header | Never persisted; provider errors sanitized to a status code | **Good in code, weak in deployment** (SEC-012) |
| Prompt / `input_payload` | Client → API → PostgreSQL JSONB → worker → provider | Excluded from logs, metrics, traces, outbox messages | **Good in transit, no encryption/retention at rest** (SEC-022) |
| Provider output | Provider → `runs.result_payload` (JSONB) | Only id/model/text/token counts retained | **Good** |
| Run/attempt IDs | Persisted; allowed in trace attributes, forbidden as metric labels | Explicit allowlist | **Good** |
| DB / Redis / AMQP URLs | Kubernetes Secret → env → `Settings` | `SecretStr` for keys; URLs are plain `AnyUrl` | Acceptable |
| Trace context | Caller header → OTel propagator → outbox → worker | Worker filters to `traceparent`/`tracestate`; API does not | **Weak at the API edge** (SEC-014) |

### Untrusted input inventory (source → sink)

| Source | Validated | Sanitized | Authorized | Encoded | Persisted |
| ------ | --------- | --------- | ---------- | ------- | --------- |
| `input` body | Size + non-empty | n/a (opaque JSON) | Scope only | Parameterized | Yes (JSONB) |
| `policy` body | Type-checked, **not bounded** | No | Scope only | Parameterized | Yes (JSONB) |
| `rules[]` body | Shape only | **No** | Scope only | n/a | Rule count only |
| `X-Client-Id` | Non-empty only | Trimmed | **Is the authorization** | Parameterized | Yes (`String(128)`) |
| `Idempotency-Key` | Strict regex | n/a | Scoped by client_id | Parameterized | Yes |
| `run_id` | UUID | n/a | Ownership check | Parameterized | n/a |
| `traceparent` | **No** | No | No | n/a | Yes (via outbox) |
| `Content-Length` | Parsed, failures ignored | n/a | n/a | n/a | No |
| AMQP message | JSON + UUID shape | Trace keys filtered | Broker creds | n/a | No |
| OpenAI response | Type-guarded per field | Errors → status code only | n/a | n/a | Partially |

---

## 7. Threat Model

### Assets

| ID | Asset | Why it matters |
| -- | ----- | -------------- |
| A-1 | Tenant run data (`input_payload`, `result_payload`, events, attempts) | Contains customer prompts and model output — likely business-confidential, potentially PII |
| A-2 | The single shared API key | The only authentication factor for the entire deployment |
| A-3 | OpenAI API key | Directly monetizable; abuse bills the operator |
| A-4 | PostgreSQL credentials | Full read/write/DDL over all tenant data |
| A-5 | RabbitMQ credentials | Ability to inject or drain execution work |
| A-6 | API availability | The runtime's entire value proposition is reliability |
| A-7 | Worker execution capacity | Finite; shared across all tenants |
| A-8 | Security audit trail | The only forensic record of boundary decisions |
| A-9 | Provider spend budget | Unbounded downside if execution volume is uncontrolled |
| A-10 | CI/CD identity and release integrity | Supply-chain root of trust |

### Threat actors

| ID | Actor | Capability | Realistic? |
| -- | ----- | ---------- | ---------- |
| TA-1 | Anonymous internet attacker | HTTP to the API | **Yes** — and fully privileged when `auth_mode=disabled` (the default) |
| TA-2 | Legitimate integrating client | Holds the shared key; sets any header | **Yes — the primary threat actor for this system** |
| TA-3 | Compromised client application | Same as TA-2, hostile intent | Yes |
| TA-4 | Malicious tenant | A customer of the operator, seeking other tenants' data | Yes |
| TA-5 | Network-adjacent pod in the cluster | No NetworkPolicy restricts it | Yes |
| TA-6 | Compromised dependency | Executes in every process | Low today (clean tree, lockfile) |
| TA-7 | Compromised CI identity | `contents: read` only, no secrets in workflows | Low — CI is well-scoped |
| TA-8 | Insider with DB access | Reads all tenant data; can disable audit triggers | Moderate |

### Abuse cases

| ID | Abuse case | Outcome | Finding |
| -- | ---------- | ------- | ------- |
| AB-1 | TA-2 changes `X-Client-Id` to another tenant's value and reads their runs, attempts and events | Cross-tenant data disclosure | SEC-001 |
| AB-2 | TA-2 replays another tenant's run, causing execution billed to that tenant's history | Cross-tenant integrity violation | SEC-001 |
| AB-3 | TA-2 submits an evaluation with `operator: "matches"` and a catastrophic-backtracking regex | API worker pinned at 100% CPU; all tenants' requests stall | SEC-002 |
| AB-4 | TA-2 posts a 100-case regression with `provider: "openai"` and no `X-Client-Id` | 200 unmetered provider calls; direct financial loss | SEC-003 |
| AB-5 | TA-2 submits a run with `max_attempts: 5000000` and `attempt_timeout_seconds: 0.001` | Self-sustaining retry storm: millions of DB transactions, AMQP messages and provider calls from one request | SEC-004 |
| AB-6 | TA-2 rotates `X-Client-Id` on every request | Rate limit never engages | SEC-006 |
| AB-7 | TA-2 sends a chunked body with no `Content-Length` | Size guard bypassed; memory amplified ~18× | SEC-005 |
| AB-8 | TA-1 sends a 200-character `X-Client-Id` while brute-forcing the key | Audit writes fail silently; the attack leaves no durable trace | SEC-007 |
| AB-9 | TA-1 fetches `/openapi.json` | Complete API map without credentials | SEC-009 |
| AB-10 | TA-8 or an attacker with a DB dump cracks the unsalted SHA-256 digest | Recovery of the shared key if it is not high-entropy | SEC-010 |
| AB-11 | An attacker achieving code execution in the API pod reads the mounted service-account token | Lateral movement to the Kubernetes API | SEC-011 |
| AB-12 | The same attacker reads `APP_OPENAI_API_KEY` from the API pod's environment | Provider key theft from a pod that never needed it | SEC-012 |
| AB-13 | Operator sets `APP_PROVIDER_TIMEOUT_SECONDS=5` believing it bounds provider calls | The setting is never read; the control does not exist | SEC-020 |
| AB-14 | A mutable third-party GitHub Action tag is repointed to malicious code | Build-time compromise | SEC-015 |

### Threats deliberately assessed and **not** found

- **Injection (SQLi/NoSQLi/command/template/LDAP/XPath):** no raw SQL anywhere (`sa.text` is never
  imported in `src/`), no `subprocess`/`os.system`, no template engine, no shell interpolation.
- **Unsafe deserialization:** only `json.loads`; no `pickle`, `marshal`, or `yaml.load`.
- **SSRF:** the only outbound URL in the runtime is `settings.openai_base_url`, which is
  operator-controlled. No endpoint accepts a URL. Verified — see §25.
- **XSS / CSRF / clickjacking:** no HTML rendering, no cookies, no browser session. Not applicable.
- **Path traversal:** the only filesystem access is in the operator-run CLI.

---

## 8. Security Findings Summary

| ID | Finding | Severity | Confidence | CWE | Component | Status |
| -- | ------- | -------- | ---------- | --- | --------- | ------ |
| SEC-001 | Tenant isolation is asserted by the caller via `X-Client-Id` | High | High | CWE-639 | API / AuthZ | Confirmed |
| SEC-002 | Caller-supplied regex and JSON Schema execute unbounded on the API event loop | High | High | CWE-1333 | Evaluation engine | Confirmed |
| SEC-003 | `/v1/evaluation-regressions` is unidentified and unmetered, driving 200 provider calls | High | High | CWE-770 | API / regression runner | Confirmed |
| SEC-004 | Caller-controlled retry policy overrides every server-side bound | High | High | CWE-770 | Policy snapshot | Confirmed |
| SEC-005 | Request-size guard trusts `Content-Length`; chunked bodies bypass it | Medium | High | CWE-770 | Security middleware | Confirmed |
| SEC-006 | Rate limiting is keyed on the caller-chosen `X-Client-Id` | Medium | High | CWE-770 | Rate limiter | Confirmed |
| SEC-007 | Security audit writes are best-effort and silently swallowed | Medium | High | CWE-778 | Audit sink | Confirmed |
| SEC-008 | Authentication defaults to `disabled` (fail-open) on a `0.0.0.0` listener | Medium | High | CWE-1188 | Settings / processes | Confirmed |
| SEC-010 | API key verifier is unsalted single-round SHA-256, stored again in audit rows | Medium | High | CWE-916 | AuthN / audit | Confirmed |
| SEC-011 | Kubernetes workloads have no `securityContext` and automount a SA token | Medium | High | CWE-1327 | Helm chart | Confirmed |
| SEC-012 | Every workload receives the full runtime Secret, including the provider key | Medium | High | CWE-250 | Helm chart | Confirmed |
| SEC-020 | `APP_PROVIDER_TIMEOUT_SECONDS` is configured everywhere but never read | Medium | High | CWE-1188 | Settings / provider | Confirmed |
| SEC-009 | OpenAPI schema and Swagger/ReDoc UI served without authentication | Low | High | CWE-200 | API | Confirmed |
| SEC-013 | Exception tracebacks are silently discarded by the JSON log formatter | Low | High | CWE-778 | Logging | Confirmed |
| SEC-014 | W3C trace context accepted unvalidated from untrusted request headers | Low | High | CWE-20 | Telemetry | Confirmed |
| SEC-015 | Most third-party GitHub Actions pinned to mutable tags; no security gates in CI | Low | High | CWE-1357 | CI/CD | Confirmed |
| SEC-016 | Container image built from a mutable base tag; no digest pinning or SBOM | Low | High | CWE-1104 | Dockerfile | Confirmed |
| SEC-017 | `X-Client-Id` length is never checked against its `VARCHAR(128)` column | Low | Medium | CWE-20 | API / persistence | Likely |
| SEC-018 | The application role owns its own schema and runs migrations | Low | High | CWE-250 | Database | Confirmed |
| SEC-019 | Terraform namespaces lack Pod Security Admission labels and NetworkPolicy | Low | High | CWE-1008 | Terraform | Confirmed |
| SEC-022 | Caller input is echoed into `result_payload`; no retention or encryption policy | Low | High | CWE-359 | Provider / persistence | Confirmed |
| SEC-021 | Local Compose stack ships weak credentials and exposes datastore ports | Informational | High | CWE-1188 | docker-compose | Confirmed |
| SEC-023 | `pytest` 8.4.2 carries PYSEC-2026-1845 (dev-only, not in the runtime image) | Informational | High | CWE-379 | Dev dependencies | Confirmed |

**A note on CVSS v4.0.** Each finding below carries a CVSS v4.0 **vector**, which is a precise and
reproducible statement of the assessment. Numeric base scores are **deliberately omitted**: CVSS v4.0
scoring is defined by an official 270-entry macrovector lookup table, and reproducing those values
from memory would risk publishing numbers that do not match the official calculator. Paste any vector
below into the FIRST CVSS v4.0 calculator to obtain the authoritative score. Severity ratings in this
report are assigned per §26 and are not derived solely from CVSS.

---

## 9. Detailed Findings

### SEC-001 — Tenant isolation is asserted by the caller, not derived from the credential

**Severity:** High
**Confidence:** High
**Status:** Confirmed
**Affected Component:** API authorization boundary
**Affected File(s):** `src/agent_runtime/api/main.py`, `src/agent_runtime/infrastructure/database/run_service.py`
**Affected Line(s):** `api/main.py:287`, `api/main.py:452`, `api/main.py:627-634`; `run_service.py:358-366`
**CWE:** CWE-639 (Authorization Bypass Through User-Controlled Key); also CWE-565 (Reliance on Cookie/Header Without Validation), CWE-284
**OWASP Mapping:** A01:2025 Broken Access Control; API1:2023 Broken Object Level Authorization
**ASVS Mapping:** V4.1.1, V4.1.3, V4.2.1 (Access Control), V1.4.4 (Access Control Architecture)
**CVSS v4.0:** `CVSS:4.0/AV:N/AC:L/AT:N/PR:L/UI:N/VC:H/VI:L/VA:N/SC:N/SI:N/SA:N`

#### Description

Every tenant-scoped operation resolves its scope from the `X-Client-Id` request header. The header is
validated only for non-emptiness and is never bound to the authenticated credential. Because
`APP_AUTH_API_KEY_HASH` configures exactly **one** key for the entire deployment, every integrating
client authenticates as the same principal and may then claim any tenant identity it likes.

The project is partly aware of this. ADR-0004 states plainly: *"`X-Client-Id` is a scoping identifier,
not an authentication factor."* That is an accurate description of the authentication layer. What the
ADR does not address is that the **authorization** layer has nothing else to stand on — every
ownership check in `SqlAlchemyRunService` filters on exactly this caller-asserted value. The
consequence is that the documented "scoping identifier" is, in practice, the entire access-control
boundary for run data.

#### Evidence

The ownership check that guards every read, evaluation and replay:

```python
# src/agent_runtime/infrastructure/database/run_service.py:358-366
@staticmethod
async def _get_run_for_client(session: AsyncSession, *, client_id: str, run_id: UUID) -> Run:
    run = cast(
        Run | None,
        await session.scalar(select(Run).where(Run.id == run_id, Run.client_id == client_id)),
    )
    if run is None:
        raise RunNotFoundError(f"Run {run_id} was not found")
    return run
```

The only validation `client_id` ever receives:

```python
# src/agent_runtime/api/main.py:627-634
def _required_client_id(client_id: str | None) -> str:
    if client_id is None or not client_id.strip():
        raise ApiProblem(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="MISSING_CLIENT_ID",
            message="X-Client-Id header is required until authentication is introduced.",
        )
    return client_id.strip()
```

The middleware authenticates the key and *separately* reads the header, never relating the two:

```python
# src/agent_runtime/api/main.py:238, 259-263
client_id = request.headers.get("X-Client-Id")
...
authentication = authenticate_api_key(
    settings=runtime_settings,
    x_api_key=request.headers.get("X-API-Key"),
    authorization=request.headers.get("Authorization"),
)
```

**Verified in-process (experiment V5).** One valid credential, two tenant scopes, both accepted:

```
same valid credential, two different tenant scopes:
  200 as tenant-alice, 200 as tenant-bob
persistence layer was asked for: [('get_run', 'tenant-alice'), ('get_run', 'tenant-bob')]
```

#### Attack Scenario

An organization integrates two of its customers, "alice" and "bob", against the runtime. Both receive
the same API key because only one exists. Alice's application — or anyone who obtains Alice's key —
changes a single header value and gains full read access to Bob's runs, attempt history, error codes,
provider selections, latencies, and the complete `run_events` metadata trail. The same substitution
works against `POST /v1/runs/{id}/replay`, letting the attacker re-execute Bob's workload and write
audit events into Bob's run history, and against `POST /v1/runs/{id}/evaluations`, letting them mutate
Bob's `evaluation_status`.

Run IDs are UUIDv4 and therefore not guessable, which raises the bar for blind enumeration. But run
IDs are not secrets in this design — they are returned to clients, printed by the demo script, echoed
in logs, and carried in trace attributes. Any leakage of a run ID (a shared log line, a support
ticket, a trace exported to a shared collector) converts directly into cross-tenant access.

#### Preconditions

- `APP_AUTH_MODE=api_key` and the attacker holds the shared key — i.e. the attacker is any
  legitimate integrating client. **Or** `APP_AUTH_MODE=disabled` (the shipped default), in which case
  no precondition exists at all.
- Knowledge of a target `run_id`, or of a target `client_id` for submission-side impersonation.

#### Security Impact

- **Confidentiality:** High — cross-tenant disclosure of run inputs' metadata, execution history, and
  event trails.
- **Integrity:** Low–Moderate — attacker can create runs attributed to another tenant, trigger replays
  in their history, and change their `evaluation_status`.
- **Availability:** Low — indirect, via consuming another tenant's apparent quota.
- **Privilege escalation:** None vertically (no admin tier exists).
- **Tenant isolation:** **Broken.** This is the finding's core impact.
- **Account takeover:** Not applicable.
- **Non-repudiation:** Weakened — `security_audit_events.client_id` records the asserted identity, so
  the audit trail attributes actions to whoever the attacker claimed to be.

#### Likelihood

**High.** No tooling, no timing, and no race is required — it is one header edit, and the header is
already present in every request the SDK sends. Any developer casually experimenting with the API will
discover it.

#### Remediation

Fix this at the **authentication layer**, not by patching individual handlers.

1. **Bind identity to the credential.** Replace the single `APP_AUTH_API_KEY_HASH` with a credential
   store that maps a key digest to a `client_id`. A minimal table is sufficient for V0:

   ```sql
   CREATE TABLE api_credentials (
       id                    uuid PRIMARY KEY,
       client_id             varchar(128) NOT NULL,
       credential_digest     varchar(128) NOT NULL UNIQUE,  -- see SEC-010 for the algorithm
       created_at            timestamptz NOT NULL DEFAULT now(),
       revoked_at            timestamptz
   );
   CREATE INDEX ix_api_credentials_digest ON api_credentials (credential_digest);
   ```

2. **Return the resolved identity from `authenticate_api_key()`.** Extend `AuthenticationResult` with
   `client_id: str | None`, populated from the credential record — never from a header.

3. **Propagate it through request state, not headers.** In `enforce_security_boundary`, set
   `request.state.client_id = authentication.client_id` and have `_required_client_id()` read from
   `request.state`. Delete every `Header(alias="X-Client-Id")` parameter from the route signatures.

4. **Treat a supplied `X-Client-Id` as a hard error**, not as a value to prefer or ignore. If the
   header is present and differs from the resolved identity, return `403` and write an audit record —
   this turns a silent bypass into a detection signal during migration.

5. **Keep the DB filter.** `_get_run_for_client` is correct and should remain as defence in depth once
   the value flowing into it is trustworthy.

For deployments that legitimately need one credential to act for several tenants (an internal control
plane, for example), model that explicitly: allow the credential record to carry a set of permitted
`client_id` values and validate the requested scope against that set — an allowlist, not an assertion.

#### Verification

1. Issue two credentials bound to `tenant-alice` and `tenant-bob`.
2. Create a run as Alice; record its `run_id`.
3. `GET /v1/runs/{alice_run_id}` with Bob's key → expect `404 RUN_NOT_FOUND`.
4. Repeat step 3 adding `X-Client-Id: tenant-alice` → expect `403`, **not** `200`.
5. Repeat for `/attempts`, `/events`, `/evaluations`, `POST /evaluations`, `POST /replay`.
6. Confirm a `security_audit_events` row exists for the `403`.

#### Regression Test

Add `tests/integration/test_tenant_isolation.py`, parametrized over all six tenant-scoped routes:

```python
@pytest.mark.parametrize(
    "method,path_suffix",
    [
        ("GET", ""),
        ("GET", "/attempts"),
        ("GET", "/events"),
        ("GET", "/evaluations"),
        ("POST", "/evaluations"),
        ("POST", "/replay"),
    ],
)
def test_credential_scope_cannot_be_overridden_by_header(method, path_suffix):
    """A credential bound to tenant B must never reach tenant A's run,
    with or without a spoofed X-Client-Id header."""
```

Assert `404`/`403` for every combination and assert the persistence layer was invoked with the
**credential-derived** client id — never the header value.

---

### SEC-002 — Caller-supplied regex and JSON Schema execute unbounded on the API event loop

**Severity:** High
**Confidence:** High
**Status:** Confirmed
**Affected Component:** Deterministic evaluation engine
**Affected File(s):** `src/agent_runtime/evaluation/engine.py`, `src/agent_runtime/infrastructure/database/run_service.py`
**Affected Line(s):** `engine.py:102` (`re.search`), `engine.py:65` (`Draft202012Validator(...).validate`); `run_service.py:207-209`
**CWE:** CWE-1333 (Inefficient Regular Expression Complexity); also CWE-400, CWE-770
**OWASP Mapping:** A03:2025 Injection (rule-language injection); API4:2023 Unrestricted Resource Consumption
**ASVS Mapping:** V5.2.4 (dynamic code/expression evaluation), V5.1.4 (input validation), V11.1.3 (anti-automation / resource limits)
**CVSS v4.0:** `CVSS:4.0/AV:N/AC:L/AT:N/PR:L/UI:N/VC:N/VI:N/VA:H/SC:N/SI:N/SA:N`

#### Description

`POST /v1/runs/{run_id}/evaluations` accepts up to 32 rules. Two rule types hand caller-controlled
data to an evaluation engine that is vulnerable to catastrophic backtracking:

- `{"type": "rule", "operator": "matches", "value": "<regex>"}` → `re.search(value, subject)`
- `{"type": "json_schema", "schema": {"pattern": "<regex>"}}` → `Draft202012Validator(...).validate()`

Python's `re` module is a backtracking engine with no timeout, no step budget, and no way to interrupt
a match in progress. A pattern such as `(a+)+$` exhibits exponential time in the subject length.

The aggravating factor is *where* this runs. `SqlAlchemyRunService.evaluate()` calls `evaluate_rules()`
inline on the API process's asyncio event loop — no thread pool, no `asyncio.to_thread`, no
`wait_for`. A blocking regex therefore does not merely slow one request: it freezes the entire event
loop, stalling every other tenant's request served by that pod until the match completes. This is
precisely the work that ADR-0002 says belongs in the worker.

#### Evidence

The regex sink, with attacker-controlled `expected`:

```python
# src/agent_runtime/evaluation/engine.py:99-106
if not isinstance(expected, str):
    raise EvaluationConfigurationError("matches rule requires a string value")
try:
    passed = found and isinstance(value, str) and re.search(expected, value) is not None
except re.error as exc:
    raise EvaluationConfigurationError("matches rule contains an invalid regex") from exc
```

The `re.error` handler catches malformed patterns — it cannot catch a *well-formed* pattern that
simply never finishes.

The schema sink, with attacker-controlled `schema`:

```python
# src/agent_runtime/evaluation/engine.py:57-70
schema = rule.get("schema")
if not isinstance(schema, dict):
    raise EvaluationConfigurationError("json_schema rule requires an object schema")
...
Draft202012Validator(schema).validate(value)
```

The inline invocation on the API event loop:

```python
# src/agent_runtime/infrastructure/database/run_service.py:206-209
try:
    outcome = evaluate_rules(
        rules=rules, result_payload=result_payload, latency_ms=latency_ms
    )
```

**Verified in-process (experiments V1, V2, V16).** Measured blocking CPU for `(a+)+$`:

```
  subject length  18  ->     0.012s blocking CPU
  subject length  20  ->     0.047s blocking CPU
  subject length  22  ->     0.186s blocking CPU
  subject length  24  ->     0.761s blocking CPU
  subject length  26  ->     2.989s blocking CPU

doubling per added character; 32 such rules are accepted per request
a 40-char subject extrapolates to ~14 hours on one event loop
```

The `json_schema` path produced identical timings, confirming both sinks are independently
exploitable. V16 confirmed no thread offload and no timeout wraps the call.

During an earlier, unbounded run of this experiment a 35-character subject did not complete within ten
minutes of continuous CPU — a single HTTP request.

#### Attack Scenario

A tenant submits one ordinary run and waits for it to reach `SUCCEEDED`. The deterministic provider
stores `{"accepted_input": {...}, "policy_applied": {...}}` as the result — and the tenant controls
that content entirely, so it can plant a subject string of whatever length it wants. The tenant then
posts an evaluation containing 32 `matches` rules, each with `(a+)+$` pointed at that subject. The
API pod's event loop stops serving anyone. Because the request is never completed, no rate-limit
window elapses meaningfully and a handful of concurrent requests across `api.replicaCount: 2` removes
the API entirely. Kubernetes liveness probes then fail (`/healthz` cannot be served by a blocked event
loop), the pod restarts, and the attacker repeats — converting a CPU exhaustion into a crash loop.

#### Preconditions

- A valid API key (or none at all under the default `auth_mode=disabled`).
- One `SUCCEEDED` run owned by the caller — trivially obtained, since the default `deterministic`
  provider always succeeds and requires no credentials.

#### Security Impact

- **Confidentiality:** None.
- **Integrity:** None.
- **Availability:** **High** — complete denial of service for the API tier, affecting all tenants.
- **Privilege escalation:** None.
- **Tenant isolation:** Violated in the availability dimension: one tenant's input halts all others.
- **Blast radius:** Per-pod, but with only two API replicas by default, two concurrent requests suffice.

#### Likelihood

**High.** The attack requires no timing, no race, and no special tooling — a single well-formed JSON
body. The pattern `(a+)+$` is textbook and widely published.

#### Remediation

Layer three independent controls; none alone is sufficient.

1. **Replace the regex engine with a non-backtracking one.** This is the only control that addresses
   the root cause. Use Google's RE2 via the `google-re2` package, which is linear-time by construction
   and rejects patterns it cannot evaluate safely:

   ```python
   import re2

   try:
       compiled = re2.compile(expected)
   except re2.error as exc:
       raise EvaluationConfigurationError("matches rule contains an unsupported regex") from exc
   passed = found and isinstance(value, str) and compiled.search(value) is not None
   ```

   If adding a native dependency is unacceptable, the fallback is to restrict `matches` to a small
   safe subset — literal substrings, anchored prefixes/suffixes, character classes without nested
   quantifiers — and reject anything containing a quantified group (`(...)+`, `(...)*`, `(...){n,}`).
   Validate the pattern at **submission** time so the caller gets a `422`, not a hung request.

2. **Move evaluation off the request path.** `evaluate()` already persists a `PENDING` evaluation
   record before doing the work and updates it afterwards — the lifecycle is *already* designed for
   asynchronous completion. Emit an outbox event instead of computing inline, let a worker run
   `evaluate_rules()`, and return `202 Accepted` with the evaluation ID. Clients poll
   `GET /v1/runs/{id}/evaluations`, exactly as they already poll for run status. This aligns the code
   with ADR-0002 and removes the API-tier blast radius entirely.

3. **Bound the work regardless.** Wherever `evaluate_rules()` finally runs, wrap it so it cannot run
   forever:

   ```python
   outcome = await asyncio.wait_for(
       asyncio.to_thread(evaluate_rules, rules=rules, ...),
       timeout=settings.evaluation_timeout_seconds,   # new bounded setting, e.g. ge=1, le=30
   )
   ```

   Note that `asyncio.to_thread` frees the event loop but does **not** kill a runaway regex — the
   thread keeps burning CPU. That is why control 1 remains mandatory; controls 2 and 3 contain the
   damage, they do not eliminate it.

4. **Bound the JSON Schema too.** Cap serialized schema size, cap nesting depth, and reject `pattern`
   and `patternProperties` values that fail the same safe-regex check as control 1.

#### Verification

1. `POST /v1/runs/{id}/evaluations` with `{"type":"rule","operator":"matches","value":"(a+)+$"}` and a
   40-character subject → expect a prompt `422` (pattern rejected) or a bounded timeout, never a hang.
2. While that request is in flight, `GET /healthz` from a second connection → must return `200`
   within its normal latency.
3. Repeat with `{"type":"json_schema","schema":{"pattern":"(a+)+$"}}`.
4. Submit 32 such rules concurrently across both API replicas and confirm p99 latency for unrelated
   endpoints is unaffected.

#### Regression Test

Add `tests/unit/test_evaluation_engine_resource_limits.py`:

```python
CATASTROPHIC = ["(a+)+$", "(a|a)+$", "(.*a){20}$", "([a-zA-Z]+)*$"]


@pytest.mark.parametrize("pattern", CATASTROPHIC)
def test_catastrophic_regex_is_rejected_or_bounded(pattern):
    """A pathological caller regex must not consume unbounded CPU."""
    subject = {"output_text": "a" * 40 + "!"}
    start = time.perf_counter()
    with pytest.raises(EvaluationConfigurationError):
        evaluate_rules(
            rules=[
                {"type": "rule", "path": "output_text", "operator": "matches", "value": pattern}
            ],
            result_payload=subject,
            latency_ms=1,
        )
    assert time.perf_counter() - start < 1.0
```

Add the equivalent for `json_schema` `pattern`, and an integration test asserting `/healthz` stays
responsive while an evaluation is in flight.

---
### SEC-003 — `/v1/evaluation-regressions` is unidentified, unmetered, and drives 200 provider calls

**Severity:** High
**Confidence:** High
**Status:** Confirmed
**Affected Component:** Evaluation regression API
**Affected File(s):** `src/agent_runtime/api/main.py`, `src/agent_runtime/evaluation/regression.py`, `src/agent_runtime/api/schemas.py`
**Affected Line(s):** `api/main.py:287` (the rate-limit guard), `api/main.py:551-575` (the route), `regression.py:135-136`, `regression.py:151-156`, `schemas.py:89`
**CWE:** CWE-770 (Allocation Without Limits or Throttling); also CWE-799 (Improper Control of Interaction Frequency), CWE-306 (Missing Authentication for Critical Function, in default mode)
**OWASP Mapping:** API4:2023 Unrestricted Resource Consumption; API6:2023 Unrestricted Access to Sensitive Business Flows; A04:2025 Insecure Design
**ASVS Mapping:** V11.1.1, V11.1.3 (business logic / anti-automation), V4.1.1 (access control on every function)
**CVSS v4.0:** `CVSS:4.0/AV:N/AC:L/AT:N/PR:L/UI:N/VC:N/VI:N/VA:H/SC:N/SI:N/SA:L`

#### Description

Three weaknesses compose into the most financially dangerous endpoint in the system.

1. **The route takes no client identity.** Unlike every other `/v1/` handler, `run_evaluation_regression`
   declares no `X-Client-Id` parameter and performs no ownership check.
2. **The rate limiter is therefore never consulted.** The middleware only invokes it when a
   `X-Client-Id` header is present and non-blank. Omitting the header skips rate limiting entirely
   while still passing authentication.
3. **The work is large and synchronous.** The dataset accepts 100 cases; the runner executes every
   case against *both* the baseline and the candidate target, inline on the API event loop. With
   `provider: "openai"`, that is up to **200 real, billable provider calls per HTTP request**.

#### Evidence

The rate-limit guard that the endpoint sidesteps by design:

```python
# src/agent_runtime/api/main.py:287-289
if runtime_settings.auth_mode == "api_key" and client_id is not None and client_id.strip():
    try:
        decision = await rate_limiter.check(client_id.strip())
```

The route — note the absence of any `client_id` parameter, in contrast to every sibling handler:

```python
# src/agent_runtime/api/main.py:556-569
async def run_evaluation_regression(
    payload: EvaluationRegressionRequest,
) -> dict[str, Any]:
    try:
        dataset = RegressionDataset.from_mapping(payload.dataset.model_dump())
        baseline = ProviderModelTarget(**payload.baseline.model_dump())
        candidate = ProviderModelTarget(**payload.candidate.model_dump())
        _validate_regression_targets(runtime_settings, baseline, candidate)
        return await _regression_runner(runtime_settings).run(...)
```

Both targets are executed in full:

```python
# src/agent_runtime/evaluation/regression.py:135-136
baseline_result = await self._run_target(dataset, baseline)
candidate_result = await self._run_target(dataset, candidate)
```

The per-case loop, with no concurrency cap, no budget, and no deadline:

```python
# src/agent_runtime/evaluation/regression.py:155-162
for case in dataset.cases:
    started_at = time.perf_counter()
    try:
        result = await self._executor.execute(
            provider=target.provider,
            input_payload=case.input_payload,
            policy_snapshot=_target_policy(target),
        )
```

The cap is 100 cases (`schemas.py:89`), and `openai` is an accepted target whenever the API process
has a key (`api/main.py:158-163`).

**Verified in-process (experiment V7):**

```
status=200 (no X-Client-Id sent)
limiter buckets consulted: []
audit records written: [('AUTHENTICATION', 'ALLOWED', None)]
provider executions driven by one unmetered request: 200
```

#### Attack Scenario

An attacker holding the shared key (or no key at all, under the default `auth_mode=disabled`) posts a
100-case dataset with `baseline.provider` and `candidate.provider` both set to `"openai"` and
`policy.model` implicitly resolving to `gpt-5`. Each request triggers 200 provider calls. The
attacker omits `X-Client-Id`, so the Redis limiter is never touched and no per-client budget applies.
They then issue these requests in a loop, in parallel, from a single host.

There is no ceiling anywhere in the path: no per-client quota, no global concurrency limit, no spend
cap, no request deadline. The bill accrues in real time against the operator's OpenAI account. In
parallel, each in-flight request occupies an API worker slot for the full duration of 200 sequential
HTTP round-trips — so the same attack also degrades API availability, and the audit trail records only
a single `AUTHENTICATION / ALLOWED` row with `client_id: None`, giving responders nothing to attribute
the spend to.

This is the textbook OWASP API6:2023 case: a sensitive business flow (paid model inference) exposed
without the anti-automation controls the flow's cost demands.

#### Preconditions

- `APP_OPENAI_API_KEY` present in the **API** process environment. The Helm chart mounts the same
  Secret into all four workloads (see SEC-012), so this holds by default in the shipped chart —
  even though the README instructs operators to set the key "only in the worker environment."
- A valid API key, or none under the default auth mode.
- With `provider: "deterministic"` the financial impact disappears but the availability impact and the
  missing rate limiting remain.

#### Security Impact

- **Confidentiality:** None.
- **Integrity:** None — the endpoint writes nothing to the database.
- **Availability:** High — API worker slots held for the duration of hundreds of upstream calls.
- **Financial:** **High and direct** — uncapped third-party spend.
- **Tenant isolation:** Not applicable (the endpoint has no tenant concept at all — which is the
  problem).
- **Non-repudiation:** Weakened — no `client_id` is recorded for the most expensive operation
  available.

#### Likelihood

**High** where the API pod holds a provider key — which is the chart's default configuration.
**Medium** where the key is genuinely worker-only, since the availability impact persists but the
financial impact does not.

#### Remediation

1. **Require an authenticated identity on this route like every other.** Once SEC-001 is fixed,
   resolve `client_id` from the credential and pass it through. In the interim, at minimum add the
   `X-Client-Id` header parameter so the existing limiter engages.

2. **Make the rate-limit guard unconditional.** The current condition is the actual bug — a missing
   header should never mean "skip the limiter." Restructure it to fail closed:

   ```python
   if runtime_settings.auth_mode == "api_key":
       limiter_key = authentication.client_id  # credential-derived, never a header
       decision = await rate_limiter.check(limiter_key)
       if not decision.allowed:
           ...  # 429
   ```

3. **Give this flow its own, much tighter budget.** A fixed-window request limit is the wrong unit
   here — the cost is per provider call, not per request. Add a separate weighted limiter that
   consumes `len(cases) * 2` tokens from a per-client provider-call budget, and reject with `429`
   when the budget is exhausted.

4. **Reduce the per-request ceiling.** Lower `EvaluationRegressionDatasetRequest.cases` from
   `max_length=100` to something defensible (10–20) for the synchronous API, and expose large
   datasets only through the operator CLI, which is already the intended path for bulk work.

5. **Move execution off the request path.** This endpoint has the same architectural problem as
   SEC-002: it performs worker-shaped work in the API. Persist a regression job, queue it through the
   existing outbox, and return `202 Accepted` with a job ID. This resolves the availability impact
   structurally and lets the worker's existing concurrency controls apply.

6. **Add a deadline and concurrency cap** inside `_run_target()` — an `asyncio.Semaphore` for
   parallelism and an overall `asyncio.wait_for` for the run — so that even an authorized regression
   cannot run unbounded.

7. **Do not put the provider key in the API pod.** See SEC-012; that change alone removes the
   financial impact of this finding.

#### Verification

1. `POST /v1/evaluation-regressions` with no `X-Client-Id` → expect `401`/`422`, not `200`.
2. With a valid identity, confirm the Redis counter for that client increments.
3. Submit a 100-case dataset → expect `422` if the new cap is lower, or `429` once the provider-call
   budget is exhausted.
4. Assert a `security_audit_events` row exists carrying the resolved `client_id`.
5. Confirm `APP_OPENAI_API_KEY` is absent from the API pod: `kubectl exec deploy/…-api -- env | grep -c OPENAI` → `0`.

#### Regression Test

```python
def test_regression_endpoint_requires_identity_and_consumes_rate_limit():
    """The most expensive endpoint must never be reachable without an
    identified, rate-limited caller."""
    limiter = RecordingRateLimiter()
    with client(limiter) as c:
        anonymous = c.post("/v1/evaluation-regressions", headers={"X-API-Key": KEY}, json=body)
        assert anonymous.status_code in (401, 422)
        assert limiter.client_ids == []

        identified = c.post("/v1/evaluation-regressions", headers=full_headers(), json=body)
        assert limiter.client_ids == ["resolved-tenant"]


def test_regression_dataset_case_count_is_capped():
    """A caller must not be able to schedule hundreds of provider calls in one request."""
```

---

### SEC-004 — Caller-controlled retry policy overrides every server-side bound

**Severity:** High
**Confidence:** High
**Status:** Confirmed
**Affected Component:** Policy snapshot construction
**Affected File(s):** `src/agent_runtime/domain/retry.py`, `src/agent_runtime/api/schemas.py`, `src/agent_runtime/infrastructure/messaging/worker.py`
**Affected Line(s):** `retry.py:104-108` (`snapshot = dict(requested)` + `setdefault`), `retry.py:144-153` (`_positive_int` / `_positive_float`), `schemas.py:19`, `worker.py:132-140`
**CWE:** CWE-770 (Allocation Without Limits or Throttling); also CWE-915 (Improperly Controlled Modification of Dynamically-Determined Object Attributes), CWE-1284 (Improper Validation of Specified Quantity in Input)
**OWASP Mapping:** A04:2025 Insecure Design; API3:2023 Broken Object Property Level Authorization; API6:2023 Unrestricted Access to Sensitive Business Flows
**ASVS Mapping:** V5.1.3, V5.1.4 (input validation and bounds), V11.1.2 (business logic limits), V13.2.2 (mass assignment)
**CVSS v4.0:** `CVSS:4.0/AV:N/AC:L/AT:N/PR:L/UI:N/VC:N/VI:L/VA:H/SC:N/SI:N/SA:L`

#### Description

`build_policy_snapshot()` starts from the caller's `policy` object verbatim and applies server defaults
only with `setdefault` — meaning a server default is used **only when the caller omitted the key**. Any
value the caller does supply wins outright.

`Settings` carefully bounds these same parameters (`retry_max_attempts` is `ge=1, le=20`;
`retry_attempt_timeout_seconds` is `gt=0, le=3600`), but those bounds constrain only the *defaults*.
The caller's values are re-validated by `_positive_int` / `_positive_float`, which enforce sign and
type — and no upper bound at all.

The result is persisted into `runs.policy_snapshot`, which the system treats as an **immutable
contract** and the worker obeys for the lifetime of the run, across every retry and every replay.

Separately, `CreateRunRequest.policy` is typed `dict[str, JsonValue]` with no schema, so arbitrary
caller-chosen keys survive into the snapshot. `policy.model`, `policy.instructions` and
`policy.max_output_tokens` are read by the OpenAI adapter — the README describes these as an
"allowlist", but the allowlisting happens at the *consumer*, not at the boundary, so unrecognised keys
are stored rather than rejected, and `max_output_tokens` has no ceiling.

#### Evidence

The merge that lets caller values win:

```python
# src/agent_runtime/domain/retry.py:104-108
snapshot = dict(requested)
snapshot.setdefault("max_attempts", max_attempts)
snapshot.setdefault("attempt_timeout_seconds", attempt_timeout_seconds)
snapshot.setdefault("initial_backoff_seconds", initial_backoff_seconds)
snapshot.setdefault("max_backoff_seconds", max_backoff_seconds)
```

The validators — sign and type only, no ceiling:

```python
# src/agent_runtime/domain/retry.py:144-153
def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return int(value)


def _positive_float(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{name} must be a positive number")
    return float(value)
```

The worker applies the caller's timeout directly, and the retry loop honours the caller's count:

```python
# src/agent_runtime/infrastructure/messaging/worker.py:132-140
retry_policy = RetryPolicy.from_snapshot(claim.policy_snapshot)
result = await asyncio.wait_for(
    self._executor.execute(...),
    timeout=retry_policy.attempt_timeout_seconds,
)
```

```python
# src/agent_runtime/infrastructure/database/execution_service.py:307
if retryable and attempt.attempt_number < retry_policy.max_attempts:
```

**Verified in-process (experiments V4, V14):**

```
Settings caps: retry_max_attempts<=20, attempt_timeout<=3600
  stored 'max_attempts'               = 5000000
  stored 'attempt_timeout_seconds'    = 86400.0
  stored 'initial_backoff_seconds'    = 1e-06
  stored 'max_backoff_seconds'        = 0.001
  stored 'max_output_tokens'          = 100000000
  stored 'model'                      = 'any-model-the-caller-names'
  stored 'instructions'               = 'attacker-controlled system prompt'
  stored 'unexpected_attacker_key'    = 'survives into the immutable snapshot'
```

```
accepted policy: max_attempts=5000000, attempt_timeout=0.001s
backoff for attempts 1,10,100 -> 1e-06s, 0.000512s, 0.001s
worker wraps execute() in asyncio.wait_for(timeout=attempt_timeout_seconds)
a 1 ms timeout always raises TimeoutError -> PROVIDER_TIMEOUT, retryable=True
=> one HTTP request schedules up to 5,000,000 attempts,
   each = 1 DB tx + 1 outbox row + 1 AMQP message + 1 provider call
```

#### Attack Scenario

**Self-sustaining retry storm.** The attacker submits a single ordinary run with:

```json
{
  "input": {"prompt": "x"},
  "policy": {
    "max_attempts": 5000000,
    "attempt_timeout_seconds": 0.001,
    "initial_backoff_seconds": 0.000001,
    "max_backoff_seconds": 0.001
  }
}
```

The API accepts it with `202`. The worker claims it and wraps the provider call in a 1 ms timeout,
which always expires. `TimeoutError` classifies as `PROVIDER_TIMEOUT`, which is retryable, so
`_schedule_retry_or_finalize` sets `RETRY_SCHEDULED` with a sub-millisecond `next_attempt_at`. The
scheduler picks it up on its next poll, writes a fresh outbox event, the dispatcher publishes it, and
the worker claims it again — five million times.

Each iteration costs one row-locking database transaction, one outbox row, one confirmed AMQP publish,
one `run_attempts` row, and one `run_events` row. The `run_events` table is append-only by trigger, so
the storage cannot be reclaimed without disabling the trigger. One HTTP request consumes worker
capacity, broker throughput, and database write capacity for all tenants — and because
`api.replicaCount` bounds the API but *nothing* bounds accepted work, the amplification factor is
effectively unbounded.

**Cost amplification variant.** With `provider_order: ["openai"]`, a generous `attempt_timeout_seconds`
and a large `max_attempts`, each retry becomes a billable inference call rather than an instant
timeout — trading throughput for direct spend.

**Property-injection variant.** `policy.instructions` is forwarded verbatim as the OpenAI `instructions`
field (the system prompt), and `policy.model` selects any model name the caller writes. A caller can
therefore choose the operator's most expensive model and control the system prompt for calls billed to
the operator's account.

#### Preconditions

- Ability to submit one run — a valid API key, or none under the default auth mode.
- For the storm variant: nothing else. The deterministic provider is sufficient; no provider
  credential is needed.

#### Security Impact

- **Confidentiality:** None.
- **Integrity:** Low — the `policy_snapshot` marketed as an immutable safety contract is in fact
  caller-authored; `run_events` is permanently polluted.
- **Availability:** **High** — database, broker and worker capacity exhausted for all tenants from a
  single accepted request.
- **Financial:** High in the OpenAI variant.
- **Tenant isolation:** Violated in the availability dimension.
- **Blast radius:** Whole-deployment, and durable — the queued work survives a restart by design.

#### Likelihood

**Medium–High.** Exploitation is one JSON body. It is rated slightly below SEC-002/SEC-003 only
because the effect is delayed and highly visible in the existing Grafana dashboard
(`arr.retries.scheduled` would spike immediately), making it more likely to be caught in progress.

#### Remediation

1. **Replace the schema-less `policy` dict with an explicit model.** This is the structural fix and
   also closes the mass-assignment gap:

   ```python
   class RunPolicyRequest(BaseModel):
       model_config = ConfigDict(extra="forbid")  # unknown keys become 422

       max_attempts: int | None = Field(default=None, ge=1, le=20)
       attempt_timeout_seconds: float | None = Field(default=None, gt=0, le=600)
       initial_backoff_seconds: float | None = Field(default=None, gt=0, le=3600)
       max_backoff_seconds: float | None = Field(default=None, gt=0, le=86_400)
       max_output_tokens: int | None = Field(default=None, ge=1, le=16_384)
       model: str | None = Field(default=None, min_length=1, max_length=128)
       instructions: str | None = Field(default=None, min_length=1, max_length=4_096)
       provider_order: list[str] | None = None
       routing: RoutingRequest | None = None
   ```

   `extra="forbid"` is already used on `CreateRunRequest` itself; applying the same discipline one
   level deeper costs nothing and eliminates the whole class.

2. **Clamp rather than trust, in `build_policy_snapshot`.** Even with the model above, the server
   should treat caller values as *requests* bounded by operator policy:

   ```python
   snapshot["max_attempts"] = min(
       _positive_int(requested.get("max_attempts", max_attempts), "max_attempts"),
       max_attempts,
   )
   snapshot["attempt_timeout_seconds"] = min(caller_timeout, attempt_timeout_seconds)
   snapshot["initial_backoff_seconds"] = max(caller_backoff, initial_backoff_seconds)
   ```

   Note the direction: attempt counts and timeouts clamp **down**, backoff clamps **up**. A caller
   must never be able to shorten a backoff below the operator's floor.

3. **Add a floor to backoff independent of the policy.** In `RetryPolicy.delay_after_attempt`, apply
   `max(delay, MINIMUM_BACKOFF_SECONDS)` with a module constant (e.g. `0.5`), so that even a
   malformed or legacy snapshot cannot produce a hot loop.

4. **Validate the model name against an allowlist.** `policy.model` should be checked against a
   configured set of permitted models, not passed through to the provider.

5. **Bound total attempts per run at the persistence layer** as a backstop, independent of the
   snapshot — a `CHECK` constraint or an explicit guard in `_schedule_retry_or_finalize` that refuses
   to schedule beyond a hard system maximum.

6. **Add a per-client concurrent/queued run quota** so that even legitimate policies cannot let one
   tenant monopolize worker capacity.

#### Verification

1. `POST /v1/runs` with `policy.max_attempts: 5000000` → expect `422`, not `202`.
2. With `policy.max_attempts: 10` and a server cap of 20 → accepted; response snapshot shows `10`.
3. With `policy.max_attempts: 100` and a server cap of 20 → accepted and **clamped to 20** (or
   rejected, per the chosen policy) — assert the persisted snapshot, not the request.
4. `policy.initial_backoff_seconds: 0.000001` → clamped up to the operator floor.
5. `policy.unexpected_key: "x"` → `422 VALIDATION_ERROR`.
6. `policy.model: "some-expensive-model"` not in the allowlist → `422`.
7. Submit a run whose attempts always time out and confirm it reaches `DEAD_LETTERED` within the
   server-capped attempt count.

#### Regression Test

```python
@pytest.mark.parametrize("policy,expected", [
    ({"max_attempts": 5_000_000}, 422),
    ({"attempt_timeout_seconds": 86_400}, 422),
    ({"initial_backoff_seconds": 1e-06}, 422),
    ({"max_output_tokens": 100_000_000}, 422),
    ({"instructions": "x" * 100_000}, 422),
    ({"unexpected_attacker_key": "x"}, 422),
])
def test_caller_policy_cannot_exceed_server_bounds(policy, expected):
    """Settings bounds must constrain caller-supplied policy, not merely the defaults."""


def test_persisted_snapshot_is_clamped_not_echoed():
    """The immutable policy snapshot must reflect server policy, not the request body."""
    snapshot = build_policy_snapshot({"max_attempts": 100}, max_attempts=20, ...)
    assert snapshot["max_attempts"] == 20


def test_backoff_has_a_hard_floor_regardless_of_snapshot():
    """A legacy or malformed snapshot must not be able to produce a hot retry loop."""
    policy = RetryPolicy(max_attempts=5, attempt_timeout_seconds=1,
                         initial_backoff_seconds=1e-9, max_backoff_seconds=1e-6,
                         provider_order=("deterministic",))
    assert policy.delay_after_attempt(1) >= MINIMUM_BACKOFF_SECONDS
```

---
### SEC-005 — Request-size guard trusts `Content-Length`; chunked bodies bypass it

**Severity:** Medium
**Confidence:** High
**Status:** Confirmed
**Affected Component:** Security middleware
**Affected File(s):** `src/agent_runtime/api/main.py`
**Affected Line(s):** `239-257`
**CWE:** CWE-770; also CWE-20 (Improper Input Validation), CWE-789 (Memory Allocation with Excessive Size Value)
**OWASP Mapping:** A04:2025 Insecure Design; API4:2023 Unrestricted Resource Consumption
**ASVS Mapping:** V11.1.4 (resource limits), V13.2.1 (request size limits)
**CVSS v4.0:** `CVSS:4.0/AV:N/AC:L/AT:N/PR:L/UI:N/VC:N/VI:N/VA:H/SC:N/SI:N/SA:N`

#### Description

`max_request_bytes` is enforced by reading the `Content-Length` header. A client that uses
`Transfer-Encoding: chunked` — or any streaming body — sends no `Content-Length`, so the check is
skipped entirely. Worse, a `Content-Length` that fails `int()` parsing is treated as *not oversized*:

```python
# src/agent_runtime/api/main.py:239-245
content_length = request.headers.get("content-length")
if content_length is not None:
    try:
        is_oversized = int(content_length) > runtime_settings.max_request_bytes
    except ValueError:
        is_oversized = False  # ← fail-open on a malformed header
```

Starlette buffers the entire request body in memory before Pydantic ever validates it, so the
downstream schema caps in `CreateRunRequest` (64 KiB input / 128 KiB request) do not prevent the
allocation — they only reject it after the memory has already been committed. `EvaluateRunRequest` and
`EvaluationRegressionRequest` have no byte-level cap at all.

#### Evidence

**Verified in-process (experiments V8, V13):**

```
with Content-Length  -> 413 (REQUEST_TOO_LARGE)
chunked, no Content-Length -> 202
```

```
configured max_request_bytes = 1024
streamed 8 MiB with no Content-Length -> HTTP 422 (not 413)
process peak RSS grew 145.7 MiB while handling it
=> the guard is advisory; the body is read before the schema ever sees it
```

An 8 MiB body produced roughly **18× memory amplification** — JSON parsing plus Pydantic model
construction — against a configured limit of 1 KiB.

#### Attack Scenario

An attacker streams multi-hundred-megabyte chunked bodies to `/v1/evaluation-regressions` (which has
no schema byte cap) in parallel. Each connection allocates its body plus parsing overhead in the API
pod, which has `limits.memory: 512Mi` in the shipped chart. The pod is OOM-killed, Kubernetes
restarts it, and the attacker repeats. `test_oversized_request_is_rejected_before_auth_or_provider_work`
passes throughout, because it only exercises the declared-`Content-Length` path.

#### Preconditions

A valid API key, or none under the default auth mode. No special tooling — `curl -T-` or any HTTP
client streaming a generator suffices.

#### Security Impact

- **Confidentiality / Integrity:** None.
- **Availability:** High — memory exhaustion and pod restart loops.
- **Blast radius:** Per-pod, all tenants on that pod.

#### Likelihood

**Medium.** Trivial to execute, but noisy and immediately visible in pod metrics.

#### Remediation

1. **Enforce the limit on bytes actually read, not on a declared header.** Wrap the ASGI receive
   channel so the counter is authoritative:

   ```python
   class MaxBodySizeMiddleware:
       def __init__(self, app, max_bytes: int) -> None:
           self.app, self.max_bytes = app, max_bytes

       async def __call__(self, scope, receive, send):
           if scope["type"] != "http":
               return await self.app(scope, receive, send)
           total = 0

           async def counting_receive():
               nonlocal total
               message = await receive()
               if message["type"] == "http.request":
                   total += len(message.get("body", b""))
                   if total > self.max_bytes:
                       raise RequestTooLarge()
               return message

           await self.app(scope, counting_receive, send)
   ```

2. **Fail closed on a malformed `Content-Length`.** Change `is_oversized = False` to reject the
   request with `400`. A header that cannot be parsed is a protocol error, not a reason to proceed.

3. **Add a byte cap to the remaining schemas.** `EvaluateRunRequest` and
   `EvaluationRegressionRequest` should carry the same `model_validator` size check that
   `CreateRunRequest` already has.

4. **Set the limit at the edge too.** Configure `client_max_body_size` (nginx) or the equivalent
   ingress annotation so oversized bodies are dropped before reaching Python at all. Defence in depth:
   the application-level control remains necessary, because the ingress is outside this repository's
   control.

#### Verification

1. Stream a 10 MiB chunked body with no `Content-Length` → expect `413`, not `202`/`422`.
2. Send `Content-Length: not-a-number` → expect `400`.
3. Send `Content-Length: 999999999` → expect `413` (existing behaviour, must not regress).
4. Monitor pod RSS during (1) and confirm it stays flat.

#### Regression Test

```python
def test_streamed_body_without_content_length_is_rejected():
    """The size guard must count bytes read, not trust a declared header."""

    def chunks():
        yield b"x" * 200_000

    response = client.post("/v1/runs", headers=auth_headers(), content=chunks())
    assert response.status_code == 413


def test_unparseable_content_length_fails_closed():
    response = client.post(
        "/v1/runs", headers={**auth_headers(), "Content-Length": "abc"}, content=b"{}"
    )
    assert response.status_code == 400
```

---

### SEC-006 — Rate limiting is keyed on the caller-chosen `X-Client-Id`

**Severity:** Medium
**Confidence:** High
**Status:** Confirmed
**Affected Component:** Redis fixed-window rate limiter
**Affected File(s):** `src/agent_runtime/api/main.py`, `src/agent_runtime/infrastructure/redis/rate_limiter.py`
**Affected Line(s):** `api/main.py:287-289`; `rate_limiter.py:37-47`
**CWE:** CWE-770; also CWE-639, CWE-837 (Improper Enforcement of a Single, Unique Action)
**OWASP Mapping:** A04:2025 Insecure Design; API4:2023 Unrestricted Resource Consumption
**ASVS Mapping:** V11.1.1, V11.1.3 (anti-automation)
**CVSS v4.0:** `CVSS:4.0/AV:N/AC:L/AT:N/PR:L/UI:N/VC:N/VI:N/VA:L/SC:N/SI:N/SA:N`

#### Description

The limiter bucket is `sha256(X-Client-Id)`. Hashing the identifier is a sensible privacy measure, but
it does nothing about provenance: the value is chosen by the caller on every request. A client that
increments an arbitrary suffix on each call gets a fresh bucket every time and is never limited. This
is the same root cause as SEC-001 — the deployment has one credential and no server-side identity —
expressed in the anti-automation control.

#### Evidence

```python
# src/agent_runtime/infrastructure/redis/rate_limiter.py:37-38
async def check(self, client_id: str) -> RateLimitDecision:
    key = f"arr:rate-limit:{hashlib.sha256(client_id.encode('utf-8')).hexdigest()}"
```

**Verified in-process (experiment V6)** — five requests under one credential, five distinct buckets:

```
limiter buckets used by ONE credential: ['rotating-identity-0', 'rotating-identity-1',
  'rotating-identity-2', 'rotating-identity-3', 'rotating-identity-4']
```

A secondary weakness: `INCR` and `EXPIRE` are issued as separate commands. If the process dies between
them the key has no TTL and persists forever, permanently locking out that bucket.

#### Attack Scenario

An attacker floods `POST /v1/runs`, incrementing `X-Client-Id` on each request. Redis accumulates one
key per request (also a slow memory-growth problem) and the limit never triggers. Every accepted run
becomes queued work for the shared worker pool. Because runs are durable by design, the backlog
survives restarts — the attacker leaves behind persistent load.

#### Preconditions

A valid API key, or none under the default auth mode.

#### Security Impact

- **Availability:** Moderate — the primary anti-automation control is inert, amplifying SEC-003,
  SEC-004 and SEC-005.
- **Integrity / Confidentiality:** None directly.
- **Blast radius:** Whole deployment.

#### Likelihood

**High** once an attacker notices the header; the change is one line of client code.

#### Remediation

1. **Key the limiter on the credential, not the header.** After SEC-001, use
   `authentication.client_id` — or, better, the credential record's primary key, so that rotating a
   key does not reset a tenant's window.
2. **Add a second dimension.** Key on `(credential, source IP)` or apply a coarser global limit, so
   that a compromised credential cannot consume the whole budget from one host.
3. **Make the window atomic.** Replace the `INCR` + `EXPIRE` pair with `SET key 0 EX <window> NX`
   followed by `INCR`, or a small Lua script, so a key can never be created without a TTL.
4. **Prefer a sliding window or token bucket.** A fixed window permits a 2× burst across the boundary;
   for provider-cost protection that matters.

#### Verification

1. Issue `limit + 1` requests with the same credential and a *different* `X-Client-Id` each time →
   the final request must return `429`.
2. Confirm Redis holds one key per credential, not one per request.
3. Kill the API mid-window and confirm the surviving key still carries a TTL (`TTL key` > 0).

#### Regression Test

```python
def test_rate_limit_bucket_is_credential_derived_not_header_derived():
    """Rotating X-Client-Id must not reset the caller's rate-limit window."""
    limiter = RecordingRateLimiter()
    with client(limiter) as c:
        for i in range(5):
            c.post("/v1/runs", headers={**auth_headers(), "X-Client-Id": f"rotate-{i}"}, json=body)
    assert len(set(limiter.client_ids)) == 1
```

---

### SEC-007 — Security audit writes are best-effort and silently swallowed

**Severity:** Medium
**Confidence:** High
**Status:** Confirmed
**Affected Component:** Security audit sink
**Affected File(s):** `src/agent_runtime/api/main.py`, `src/agent_runtime/security/audit.py`
**Affected Line(s):** `api/main.py:225-232`
**CWE:** CWE-778 (Insufficient Logging); also CWE-390 (Detection of Error Condition Without Action), CWE-703
**OWASP Mapping:** A09:2025 Security Logging and Monitoring Failures
**ASVS Mapping:** V7.1.1, V7.2.1, V7.2.2 (security event logging), V7.3.3 (log integrity)
**CVSS v4.0:** `CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:N/VI:L/VA:N/SC:N/SI:N/SA:N`

#### Description

`write_security_audit` wraps the sink in a bare `except Exception` and continues on failure. The
request proceeds and returns its normal response; only a terse line reaches stdout — and that line
carries no client ID, no event type, and no reason, so it cannot substitute for the lost record.

This turns the audit trail into a **defeatable** control. `security_audit_events.client_id` is
`String(128)`, and the API never validates the length of `X-Client-Id` (see SEC-017). A
199-character header therefore makes PostgreSQL reject every audit insert for that request with
`value too long for type character varying(128)` — while the request itself succeeds or fails
normally.

#### Evidence

```python
# src/agent_runtime/api/main.py:225-232
except Exception:
    logging.getLogger(__name__).error(
        "security audit persistence failed",
        extra={
            "event": "SECURITY_AUDIT_PERSIST_FAILED",
            "error_code": "AUDIT_WRITE_FAILED",
        },
    )
```

```python
# src/agent_runtime/infrastructure/database/models.py:204
client_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
```

**Verified in-process (experiment V9):**

```
security audit persistence failed
denied request returned 403; audit attempts=1, persisted=0
DB column is String(128); API never length-checks X-Client-Id
```

The `403` was returned to the caller with no durable record of the failed authentication.

#### Attack Scenario

An attacker brute-forces the shared API key, sending a 199-character `X-Client-Id` on every attempt.
Each attempt returns `403` as normal, but **no `AUTHENTICATION / DENIED` row is ever written**. The
operator's audit table — the system's only forensic record of boundary decisions, deliberately made
append-only by a database trigger — shows nothing. Detection depends entirely on noticing a burst of
`SECURITY_AUDIT_PERSIST_FAILED` log lines, which carry no attacker-identifying data at all.

A transient database outage produces the same effect unintentionally: the boundary keeps making
decisions while recording none of them.

#### Preconditions

None. This is reachable pre-authentication, by any anonymous caller.

#### Security Impact

- **Confidentiality:** None.
- **Integrity:** Low — the audit trail's completeness is compromised.
- **Availability:** None.
- **Non-repudiation / detection:** **Significant.** The control most needed during an incident is the
  one an attacker can turn off.

#### Likelihood

**Medium.** The suppression is not obvious from outside, but an attacker probing header handling will
find it, and the failure mode also occurs accidentally.

#### Remediation

1. **Validate `X-Client-Id` at the boundary** so it can never exceed the column width. This alone
   closes the attacker-controlled path:

   ```python
   CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
   ```

2. **Truncate defensively in the sink.** Even with (1), `SqlAlchemySecurityAuditSink.record` should
   truncate `client_id` and `reason` to their column widths, so no future caller can reintroduce the
   failure.

3. **Decide the failure policy explicitly, and document it.** Two defensible options:
   - *Fail closed* for `DENIED` outcomes — if the denial cannot be recorded, return `503`. Strongest,
     but couples availability to the audit store.
   - *Fail open with a durable fallback* — write to a bounded in-memory buffer flushed by a
     background task, and emit a `SECURITY_AUDIT_PERSIST_FAILED` metric. Preserves availability while
     making the gap visible.

   Either is acceptable; silently continuing is not.

4. **Make the failure alertable.** Add an OTel counter (`arr.security.audit_failures`) and a Grafana
   alert on any non-zero rate. A log line nobody queries is not a detection control.

5. **Log enough to be useful.** The current fallback line should carry the event type, outcome, reason
   and a truncated client ID — all already on the allowlist or trivially addable.

#### Verification

1. Send `X-Client-Id` of 200 characters with an invalid key → expect `400`/`422` (rejected by the new
   pattern) and a persisted audit row.
2. Point the sink at an unavailable database → confirm the configured policy (either `503` or
   buffered-write + metric), never a silent success.
3. Confirm `arr.security.audit_failures` increments.

#### Regression Test

```python
def test_oversized_client_id_cannot_suppress_the_audit_trail():
    """An attacker must not be able to disable security auditing via a header."""
    audit = RecordingAuditSink()
    with client(audit) as c:
        r = c.post("/v1/runs", headers={"X-API-Key": "wrong", "X-Client-Id": "x" * 200}, json=body)
    assert r.status_code in (400, 422, 403)
    assert len(audit.records) >= 1
    assert all(len(rec.client_id or "") <= 128 for rec in audit.records)


def test_audit_sink_failure_is_surfaced_not_swallowed():
    """A failing audit sink must raise a metric and follow the documented failure policy."""
```

---

### SEC-008 — Authentication defaults to `disabled` on a `0.0.0.0` listener

**Severity:** Medium
**Confidence:** High
**Status:** Confirmed
**Affected Component:** Settings and API process bootstrap
**Affected File(s):** `src/agent_runtime/settings.py`, `src/agent_runtime/processes.py`, `docker-compose.yml`, `.env.example`
**Affected Line(s):** `settings.py:24`; `processes.py:31-38`; `docker-compose.yml:13`; `.env.example:8`
**CWE:** CWE-1188 (Insecure Default Initialization); also CWE-276 (Incorrect Default Permissions), CWE-306
**OWASP Mapping:** A05:2025 Security Misconfiguration; API8:2023 Security Misconfiguration
**ASVS Mapping:** V1.1.4 (secure defaults), V14.1.1 (secure build/deploy configuration)
**CVSS v4.0:** `CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N` *(applies only to a deployment that inherits the default)*

#### Description

`auth_mode` defaults to `"disabled"`. In that mode `authenticate_api_key()` returns success
immediately, the rate limiter is a no-op, and no audit records are written — the entire security
boundary is inert. The API simultaneously binds `0.0.0.0:8000` with no TLS.

```python
# src/agent_runtime/settings.py:24
auth_mode: Literal["disabled", "api_key"] = "disabled"
```

```python
# src/agent_runtime/security/authentication.py:24-25
if settings.auth_mode == "disabled":
    return AuthenticationResult(True, None, None)
```

```python
# src/agent_runtime/processes.py:31-38
uvicorn.run("agent_runtime.api.main:app", host="0.0.0.0", port=8000, ...)
```

The Helm chart sets `config.authMode: api_key` — a genuinely good decision that makes the *supported*
production path secure by default. But `docker-compose.yml` and `.env.example` both pin
`APP_AUTH_MODE=disabled`, and those are the artifacts a developer copies. Any deployment that starts
from Compose, or that runs the container without setting `APP_AUTH_MODE`, is fully open.

This is properly a **defaults** problem rather than a vulnerability in its own right: the project
documents the risk clearly in `SECURITY.md`, `README.md` and ADR-0004. The gap is that documentation
is the only thing standing between the default configuration and an unauthenticated production API.

#### Attack Scenario

A team runs the container in a staging environment reachable from a corporate VPN, without setting
`APP_AUTH_MODE`. Every endpoint is anonymous, and because tenant scope is a header (SEC-001), any
user on that network can read, create, replay and evaluate every run in the system. Every other
finding in this report escalates: SEC-002, SEC-003, SEC-004 and SEC-005 all drop from `PR:L` to
`PR:N`.

#### Security Impact

Confidentiality, Integrity and Availability all High — but only for deployments that inherit the
default. **Likelihood: Medium**, mitigated by clear documentation and a correct Helm default.

#### Remediation

1. **Invert the default.** Make `api_key` the default and require an explicit
   `APP_AUTH_MODE=disabled` for local work. `Settings.model_post_init` already raises when
   `auth_mode == "api_key"` without a hash, so a bare container would fail fast and loudly — the
   correct behaviour.
2. **Refuse `disabled` outside development.** Add an `APP_ENVIRONMENT` setting and have
   `model_post_init` raise when `auth_mode == "disabled"` and the environment is not `local`.
3. **Log a startup warning.** Whenever `disabled` is active, emit a `WARNING` on every boot naming the
   risk — cheap, and it surfaces in any log aggregator.
4. **Bind to `127.0.0.1` in disabled mode.** If authentication is off, the process should not be
   listening on all interfaces.
5. **Add a startup banner or `/healthz` field** reporting the active auth mode, so operators can
   verify posture without reading the environment.

#### Verification

1. Start the container with no `APP_AUTH_MODE` → expect either `api_key` enforcement or a hard startup
   failure.
2. Set `APP_AUTH_MODE=disabled` with `APP_ENVIRONMENT=production` → expect a startup failure.
3. `GET /healthz` → reports the active auth mode.

#### Regression Test

```python
def test_authentication_is_enabled_by_default():
    """A deployment that sets no auth configuration must not be anonymous."""
    with pytest.raises(ValidationError):
        Settings()  # api_key default + missing hash => fail fast


def test_disabled_auth_is_rejected_outside_local_environment():
    with pytest.raises(ValidationError):
        Settings(auth_mode="disabled", environment="production")
```

---

### SEC-010 — API key verifier is unsalted single-round SHA-256, stored again in audit rows

**Severity:** Medium
**Confidence:** High
**Status:** Confirmed
**Affected Component:** API key authentication and audit
**Affected File(s):** `src/agent_runtime/security/authentication.py`, `src/agent_runtime/api/main.py`
**Affected Line(s):** `authentication.py:30-36`; `api/main.py:318-325`
**CWE:** CWE-916 (Use of Password Hash With Insufficient Computational Effort); also CWE-522, CWE-759 (Use of a One-Way Hash without a Salt)
**OWASP Mapping:** A02:2025 Cryptographic Failures; A07:2025 Identification and Authentication Failures
**ASVS Mapping:** V6.2.2, V6.2.3 (credential storage), V3.2.1 (session/credential lifecycle)
**CVSS v4.0:** `CVSS:4.0/AV:N/AC:H/AT:P/PR:N/UI:N/VC:H/VI:H/VA:L/SC:N/SI:N/SA:N`

#### Description

Two related weaknesses.

**Insufficient computational effort.** The stored verifier is a single unsalted SHA-256 of the raw key.
An attacker who obtains `APP_AUTH_API_KEY_HASH` — from a Secret dump, a leaked ConfigMap, a CI log, or
the database — can attempt offline recovery at commodity GPU rates. If the key is a high-entropy
random string this is infeasible and the finding is largely theoretical; nothing in the codebase,
documentation, or `.env.example` requires or checks that, and the example value
(`sha256-of-a-real-api-key`) offers no guidance on entropy.

**The verifier is duplicated into the audit table.** On every successful authentication the middleware
persists `credential_fingerprint`, which is computed as `sha256(raw_key)` — byte-for-byte identical to
the configured `APP_AUTH_API_KEY_HASH`. A credential verifier that was meant to live only in a Secret
is therefore copied into an application table on every request.

#### Evidence

```python
# src/agent_runtime/security/authentication.py:30-36
fingerprint = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
configured_hash = settings.auth_api_key_hash
if configured_hash is None:
    raise RuntimeError("API-key authentication has no configured credential hash")
if not hmac.compare_digest(fingerprint, configured_hash.get_secret_value()):
    return AuthenticationResult(False, fingerprint, "INVALID_CREDENTIAL")
return AuthenticationResult(True, fingerprint, None)
```

**Verified in-process (experiments V11, V17):**

```
APP_AUTH_API_KEY_HASH        = 53b976a358f9b8db...
stored credential_fingerprint = 53b976a358f9b8db...
identical: True
plain unsalted SHA-256, no KDF -> PASS(vuln confirmed)
```

```
2000 failed authentications in 0.001s
hmac.compare_digest used for the comparison -> constant time: OK (no finding)
```

The comparison itself is correct — `hmac.compare_digest` is exactly right, and the raw key is never
stored, logged, or traced. Those parts of the design are sound.

A third observation: on a *failed* authentication the fingerprint of whatever the caller submitted is
persisted. If a client misconfigures its integration and sends a different system's credential, that
credential's digest is durably recorded in an append-only table that cannot be deleted by design.

#### Attack Scenario

An attacker obtains a database backup or gains read access to `security_audit_events` — a lower bar
than reading the Kubernetes Secret, since it is ordinary application data. They now hold the exact
value of the credential verifier. If the shared key is human-chosen or drawn from a small keyspace,
offline cracking recovers it, yielding full API access — and, because of SEC-001, access to every
tenant's data.

#### Preconditions

Read access to either the Secret or the audit table, plus a key with recoverable entropy.

#### Security Impact

- **Confidentiality / Integrity:** High if the key is recovered (full API access).
- **Availability:** Low.
- **Attack complexity:** High — requires a prior compromise and a weak key.

#### Likelihood

**Low–Medium.** Contingent on both a data exposure and a low-entropy key.

#### Remediation

1. **Use a KDF for the verifier.** Move to HMAC-SHA-256 with a server-side pepper, or scrypt/Argon2id
   if per-credential latency is acceptable. With the credential table proposed in SEC-001, a
   per-credential random salt becomes natural:

   ```python
   digest = hashlib.scrypt(raw_key.encode(), salt=record.salt, n=2**14, r=8, p=1, dklen=32)
   ```

2. **Stop storing the verifier in audit rows.** Store a *truncated, domain-separated* fingerprint that
   cannot reconstruct the verifier — for example the first 12 hex characters of
   `HMAC-SHA256(audit_pepper, raw_key)`. That preserves the operational value (correlating repeated
   use of the same wrong credential) without duplicating the secret.

3. **Require and document minimum key entropy.** Specify ≥256 bits from a CSPRNG, provide a generation
   command in the README, and reject short keys at the boundary:

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

4. **Support rotation.** Allow a list of active credential records so a key can be rotated without
   downtime — ADR-0004 explicitly defers this, and it should be revisited alongside SEC-001.

#### Verification

1. Confirm `security_audit_events.credential_fingerprint` values no longer equal
   `sha256(raw_key)` and cannot be used to verify a candidate key offline.
2. Confirm a key shorter than the configured minimum is rejected at startup.
3. Confirm authentication still succeeds and remains constant-time (SEC-017 verification).
4. Confirm two active credentials can authenticate simultaneously during a rotation window.

#### Regression Test

```python
def test_audit_fingerprint_cannot_reconstruct_the_stored_verifier():
    """An audit row must never contain the value that verifies a credential."""
    result = authenticate_api_key(settings=settings, x_api_key=RAW_KEY, authorization=None)
    assert result.credential_fingerprint != settings.auth_api_key_hash.get_secret_value()
    assert result.credential_fingerprint != hashlib.sha256(RAW_KEY.encode()).hexdigest()


def test_low_entropy_api_key_is_rejected_at_configuration_time():
    with pytest.raises(ValidationError):
        Settings(auth_mode="api_key", auth_api_key_hash=sha256_of("short"), min_key_bits=256)
```

---
### SEC-011 — Kubernetes workloads have no `securityContext` and automount a service-account token

**Severity:** Medium
**Confidence:** High
**Status:** Confirmed
**Affected Component:** Helm chart
**Affected File(s):** `charts/agent-reliability-runtime/templates/workloads.yaml`, `templates/migration-job.yaml`, `templates/serviceaccount.yaml`
**Affected Line(s):** `workloads.yaml:21-39` (pod spec), `migration-job.yaml:20-33`
**CWE:** CWE-1327 (Binding to an Unrestricted IP Address / over-permissive runtime); also CWE-250 (Execution with Unnecessary Privileges), CWE-276
**OWASP Mapping:** A05:2025 Security Misconfiguration; API8:2023 Security Misconfiguration
**ASVS Mapping:** V14.1.1, V14.2.1 (deployment hardening)
**CVSS v4.0:** `CVSS:4.0/AV:L/AC:L/AT:P/PR:L/UI:N/VC:L/VI:L/VA:L/SC:H/SI:H/SA:L` *(post-compromise escalation vector)*

#### Description

Neither the Deployment template nor the migration Job declares any `podSecurityContext` or container
`securityContext`, and neither disables service-account token automounting. The pod specs contain
only `serviceAccountName`, `imagePullSecrets`, `containers`, `resources` and probes.

Missing controls:

| Control | Present? | Consequence |
| ------- | -------- | ----------- |
| `runAsNonRoot: true` | No | Nothing *enforces* non-root. The Dockerfile's `USER runtime` is the only barrier, and any image rebuild or `securityContext` override silently removes it. |
| `allowPrivilegeEscalation: false` | No | setuid binaries in the base image can escalate. |
| `readOnlyRootFilesystem: true` | No | An attacker with code execution can write to the container filesystem and persist. |
| `capabilities: drop: [ALL]` | No | The container retains the default Docker capability set. |
| `seccompProfile: RuntimeDefault` | No | Full syscall surface available to the container. |
| `automountServiceAccountToken: false` | No | A valid Kubernetes API token is mounted at `/var/run/secrets/kubernetes.io/serviceaccount/token`. |

The last item is the most consequential. None of the four processes calls the Kubernetes API — they
talk only to PostgreSQL, Redis, RabbitMQ, the OTel collector, and OpenAI. The token is pure attack
surface: any code execution in a pod (via a future dependency compromise, for instance) immediately
yields authenticated cluster API access scoped to whatever that ServiceAccount can do.

#### Evidence

```yaml
# charts/agent-reliability-runtime/templates/workloads.yaml:21-30 — the complete pod spec
spec:
  serviceAccountName: {{ include "agent-reliability-runtime.serviceAccountName" $root }}
  {{- with $root.Values.imagePullSecrets }}
  imagePullSecrets:
    {{- toYaml . | nindent 8 }}
  {{- end }}
  containers:
    - name: {{ .component }}
      image: "{{ $root.Values.image.repository }}:{{ ... }}"
      imagePullPolicy: {{ $root.Values.image.pullPolicy }}
```

The ServiceAccount is created with no `automountServiceAccountToken: false`:

```yaml
# charts/agent-reliability-runtime/templates/serviceaccount.yaml:1-11
{{- if .Values.serviceAccount.create }}
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ include "agent-reliability-runtime.serviceAccountName" . }}
```

The Dockerfile does set a non-root user — the image is well built; the orchestration simply does not
enforce it:

```dockerfile
# docker/Dockerfile:9-10, 24-25
RUN groupadd --system runtime \
    && useradd --system --gid runtime --home-dir /app --shell /usr/sbin/nologin runtime
...
RUN chown -R runtime:runtime /app
USER runtime
```

#### Attack Scenario

This is an escalation multiplier rather than an initial access vector. Given any code execution in an
API or worker pod — a future deserialization bug, a compromised transitive dependency, or a
vulnerability in an upstream library — the attacker finds: a writable root filesystem for persistence,
the full default capability set, no seccomp filter, and a mounted Kubernetes API token enabling
lateral movement to other workloads in the namespace. The blast radius expands from "one process" to
"everything that ServiceAccount can reach."

#### Preconditions

Code execution inside a pod. No such vector exists in the current code — this finding raises the cost
of a *future* one.

#### Security Impact

- **Confidentiality / Integrity / Availability (this pod):** Low incremental.
- **Subsequent system (cluster):** **High** — lateral movement and persistence.
- **Blast radius:** Namespace-wide.

#### Likelihood

**Low** today (no known execution vector), **High conditional** — if a vector ever appears, this
configuration guarantees it escalates.

#### Remediation

Add to both `workloads.yaml` and `migration-job.yaml`:

```yaml
spec:
  serviceAccountName: {{ include "agent-reliability-runtime.serviceAccountName" $root }}
  automountServiceAccountToken: false
  securityContext:
    runAsNonRoot: true
    runAsUser: 1000
    runAsGroup: 1000
    fsGroup: 1000
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: {{ .component }}
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities:
          drop: ["ALL"]
      volumeMounts:
        - name: tmp
          mountPath: /tmp
  volumes:
    - name: tmp
      emptyDir: {}
```

Notes:

- `readOnlyRootFilesystem: true` requires the writable `/tmp` `emptyDir` shown above; verify the
  OTel SDK and `uv`-installed packages do not write elsewhere before enabling it.
- Confirm the numeric UID matches the image's `runtime` user (`docker run --rm <image> id -u`), or
  make it a chart value.
- Expose these as `values.yaml` entries (`podSecurityContext`, `securityContext`) with the hardened
  values as defaults, so operators can adjust without forking the template.
- Add a `PodDisruptionBudget` for the API and worker while editing the chart — availability rather
  than security, but the same change window.

#### Verification

```bash
kubectl get pod -l app.kubernetes.io/component=api -o jsonpath='{.items[0].spec.securityContext}'
kubectl exec deploy/arr-api -- id -u                         # expect non-zero
kubectl exec deploy/arr-api -- touch /root/x                 # expect "Read-only file system"
kubectl exec deploy/arr-api -- ls /var/run/secrets/kubernetes.io/   # expect "No such file"
```

#### Regression Test

Add a `helm template | conftest`/`kubeconform` policy step to CI asserting that every rendered pod
spec sets `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`,
`capabilities.drop: [ALL]`, and `automountServiceAccountToken: false`. The `helm-lint` job already
renders the chart, so the assertion is a single added step.

---

### SEC-012 — Every workload receives the full runtime Secret, including the provider key

**Severity:** Medium
**Confidence:** High
**Status:** Confirmed
**Affected Component:** Helm chart secret distribution
**Affected File(s):** `charts/agent-reliability-runtime/templates/workloads.yaml`, `templates/migration-job.yaml`
**Affected Line(s):** `workloads.yaml:33-37`; `migration-job.yaml:31-33`
**CWE:** CWE-250 (Execution with Unnecessary Privileges); also CWE-522 (Insufficiently Protected Credentials), CWE-1394
**OWASP Mapping:** A05:2025 Security Misconfiguration; A01:2025 Broken Access Control (least privilege)
**ASVS Mapping:** V14.1.3 (least privilege in deployment), V6.4.1 (secret management)
**CVSS v4.0:** `CVSS:4.0/AV:L/AC:L/AT:P/PR:L/UI:N/VC:H/VI:L/VA:N/SC:L/SI:N/SA:N`

#### Description

All four Deployments and the migration Job mount the same Secret wholesale via `envFrom.secretRef`.
Each process therefore receives every credential the system uses, regardless of need:

| Process | Needs | Also receives |
| ------- | ----- | ------------- |
| api | DB, Redis, auth hash | **`APP_OPENAI_API_KEY`**, RabbitMQ |
| dispatcher | DB, RabbitMQ | `APP_OPENAI_API_KEY`, Redis, auth hash |
| scheduler | DB | `APP_OPENAI_API_KEY`, RabbitMQ, Redis, auth hash |
| worker | DB, RabbitMQ, `APP_OPENAI_API_KEY` | Redis, auth hash |
| migrate (Job) | DB (DDL) | Everything |

This directly contradicts the project's own documented intent. The README states: *"set
`APP_OPENAI_API_KEY` only in the worker environment."* The shipped chart makes that impossible.

The consequence is not merely theoretical least-privilege hygiene — it is the precondition that turns
SEC-003 from an availability problem into a financial one. `_regression_runner()` constructs an
`OpenAIResponsesProvider` from `settings.openai_api_key` **inside the API process**, and
`_validate_regression_targets` permits `provider: "openai"` precisely when the API process has a key.
With the chart's current secret distribution, it always does.

#### Evidence

```yaml
# charts/agent-reliability-runtime/templates/workloads.yaml:33-37
envFrom:
  - configMapRef:
      name: {{ include "agent-reliability-runtime.fullname" $root }}-config
  - secretRef:
      name: {{ required "existingSecret.name is required" $root.Values.existingSecret.name }}
```

```python
# src/agent_runtime/api/main.py:143-155 — the API builds a live OpenAI client
def _regression_runner(settings: Settings) -> EvaluationRegressionRunner:
    return EvaluationRegressionRunner(
        ProviderRegistry(
            [
                DeterministicProvider(),
                OpenAIResponsesProvider(
                    api_key=settings.openai_api_key,
                    base_url=str(settings.openai_base_url),
                    default_model=settings.openai_default_model,
                ),
            ]
        )
    )
```

The same over-provisioning applies to the migration Job, which holds a DDL-capable database credential
that the long-running processes then share (see SEC-018).

#### Attack Scenario

Any read access to the API pod's environment — a debug endpoint, a container exec, a compromised
sidecar, a crash dump, or an `env`-printing library — exposes the OpenAI key even though the API's
documented role never uses it. Combined with SEC-003, an attacker does not even need pod access: they
simply call `/v1/evaluation-regressions` with `provider: "openai"` and spend the key through the API's
own intended interface.

#### Preconditions

Deployment via the shipped Helm chart with `APP_OPENAI_API_KEY` in the Secret.

#### Security Impact

- **Confidentiality:** High for the provider credential.
- **Integrity / Availability:** Low directly.
- **Financial:** High, as the enabling condition for SEC-003.
- **Blast radius:** Any single compromised pod exposes all credentials.

#### Likelihood

**Medium** — it is the chart's default behaviour, so every deployment inherits it.

#### Remediation

1. **Split the Secret by role.** Create `…-secrets-common` (DB, Redis, RabbitMQ, auth hash) and
   `…-secrets-provider` (`APP_OPENAI_API_KEY`), and mount the provider Secret only into the worker:

   ```yaml
   envFrom:
     - configMapRef:
         name: {{ include "agent-reliability-runtime.fullname" $root }}-config
     - secretRef:
         name: {{ $root.Values.existingSecret.common }}
     {{- if .needsProvider }}
     - secretRef:
         name: {{ $root.Values.existingSecret.provider }}
     {{- end }}
   ```

   Pass `"needsProvider" true` only from the worker `include` on line 72.

2. **Use per-component `env` with `secretKeyRef`** instead of wholesale `envFrom`. More verbose, but
   it makes each workload's credential needs explicit and reviewable — and prevents a future Secret
   key from being silently distributed everywhere.

3. **Give the migration Job its own DDL credential** (see SEC-018), separate from the runtime role.

4. **Remove the OpenAI provider from the API process entirely.** Once SEC-003's execution moves to a
   worker, `_regression_runner` should not exist in `api/main.py` at all — which makes the key
   unnecessary there by construction rather than by configuration.

#### Verification

```bash
kubectl exec deploy/arr-api        -- printenv | grep -c OPENAI   # expect 0
kubectl exec deploy/arr-dispatcher -- printenv | grep -c OPENAI   # expect 0
kubectl exec deploy/arr-scheduler  -- printenv | grep -c OPENAI   # expect 0
kubectl exec deploy/arr-worker     -- printenv | grep -c OPENAI   # expect 1
```

Then confirm `POST /v1/evaluation-regressions` with `provider: "openai"` returns
`422 … requires APP_OPENAI_API_KEY` from the API.

#### Regression Test

Add to the `helm-lint` CI job:

```bash
helm template arr charts/agent-reliability-runtime \
  | yq 'select(.kind=="Deployment" and .metadata.name != "*-worker")
        | .spec.template.spec.containers[].envFrom[].secretRef.name' \
  | grep -qv provider
```

---

### SEC-020 — `APP_PROVIDER_TIMEOUT_SECONDS` is configured everywhere but never read

**Severity:** Medium
**Confidence:** High
**Status:** Confirmed
**Affected Component:** Settings and OpenAI provider adapter
**Affected File(s):** `src/agent_runtime/settings.py`, `src/agent_runtime/providers/openai_responses.py`, `docker-compose.yml`, `.env.example`, `charts/agent-reliability-runtime/templates/configmap.yaml`
**Affected Line(s):** `settings.py:30` (declared); `openai_responses.py:41` (no timeout passed); `configmap.yaml:12`; `docker-compose.yml:15`; `.env.example:15`
**CWE:** CWE-1188 (Insecure Default Initialization); also CWE-665 (Improper Initialization), CWE-1059
**OWASP Mapping:** A04:2025 Insecure Design; A05:2025 Security Misconfiguration
**ASVS Mapping:** V11.1.4 (resource limits), V1.14.6 (configuration integrity)
**CVSS v4.0:** `CVSS:4.0/AV:N/AC:H/AT:P/PR:N/UI:N/VC:N/VI:N/VA:L/SC:N/SI:N/SA:N`

#### Description

`provider_timeout_seconds` is declared in `Settings` with careful bounds (`ge=1, le=600`), shipped in
`.env.example`, set in `docker-compose.yml`, and rendered into the Helm ConfigMap as
`APP_PROVIDER_TIMEOUT_SECONDS`. It is referenced in exactly two places in the entire repository: its
own declaration, and a unit test that asserts it round-trips through `Settings`.

**No code path reads it.** `OpenAIResponsesProvider` constructs `httpx.AsyncClient(transport=...)`
with no `timeout` argument, so the effective per-request timeout is httpx's implicit 5-second default
— unchanged whether the operator sets the value to 1 or to 600.

This is a security-relevant control that appears to exist and does not. An operator tuning
`APP_PROVIDER_TIMEOUT_SECONDS` to bound provider exposure gets no effect and no warning, and will
reasonably believe the control is in place.

#### Evidence

```python
# src/agent_runtime/settings.py:30
provider_timeout_seconds: int = Field(default=60, ge=1, le=600)
```

```python
# src/agent_runtime/providers/openai_responses.py:41 — no timeout argument
async with httpx.AsyncClient(transport=self._transport) as client:
    try:
        response = await client.post(
            f"{self._base_url}/responses", headers=headers, json=body
        )
```

**Verified by exhaustive search:**

```
=== is provider_timeout_seconds ever read? ===
src/agent_runtime/settings.py:30:    provider_timeout_seconds: int = Field(default=60, ge=1, le=600)
tests/unit/test_settings.py:13:        provider_timeout_seconds=45,
tests/unit/test_settings.py:17:    assert settings.provider_timeout_seconds == 45
```

Note the attempt-level timeout (`retry_attempt_timeout_seconds`, applied via `asyncio.wait_for` in the
worker) *is* wired up and does bound worker execution. But it is caller-overridable (SEC-004) and does
not apply to the regression endpoint's provider calls at all — which run in the API process, outside
the worker, and are therefore bounded only by httpx's 5-second default.

#### Attack Scenario

Less an attack than a control failure that enables others. An operator responding to SEC-003 by
lowering `APP_PROVIDER_TIMEOUT_SECONDS` to bound regression-endpoint exposure would see no change, and
would close the incident believing it mitigated. Conversely, an operator raising it to 600 for
legitimate long-running inference would find calls still failing at 5 seconds with no explanation.

Also note that a new `httpx.AsyncClient` is created per provider call rather than reused, so no
connection pooling occurs — a performance rather than security issue, but worth fixing in the same
change.

#### Preconditions

None.

#### Security Impact

- **Availability:** Low direct impact (the implicit 5-second default is conservative), but the absence
  of an effective, operator-controlled bound weakens the response to SEC-003 and SEC-004.
- **Operational integrity:** A configuration surface that silently does nothing undermines trust in
  the rest of the configuration.

#### Likelihood

**High** that the misconception exists (the setting is documented and shipped in three places);
**Low** that it is directly exploited.

#### Remediation

1. **Wire the setting through to the adapter:**

   ```python
   class OpenAIResponsesProvider:
       def __init__(
           self,
           *,
           api_key,
           base_url,
           default_model,
           timeout_seconds: float = 60.0,
           transport: httpx.AsyncBaseTransport | None = None,
       ) -> None:
           ...
           self._timeout = httpx.Timeout(timeout_seconds, connect=min(10.0, timeout_seconds))
   ```

   and pass `timeout=self._timeout` to `AsyncClient`. Update both construction sites
   (`processes.py:96` and `api/main.py:148`) to pass `settings.provider_timeout_seconds`.

2. **Reuse the client.** Construct one `httpx.AsyncClient` per adapter instance and close it on
   shutdown, rather than one per call.

3. **Add a configuration-coverage guard.** A small test that asserts every `Settings` field is
   referenced somewhere in `src/` outside `settings.py` would have caught this at authoring time and
   will prevent recurrence:

   ```python
   def test_every_setting_is_consumed_by_the_runtime():
       """A configuration knob that nothing reads is a control that does not exist."""
   ```

#### Verification

1. Set `APP_PROVIDER_TIMEOUT_SECONDS=2` and point the adapter at a deliberately slow local stub →
   confirm the call aborts at ~2s, not ~5s.
2. Set it to `120` and confirm a 10-second response succeeds where it previously failed.
3. Confirm the attempt-level `asyncio.wait_for` still fires independently.

#### Regression Test

```python
async def test_provider_timeout_setting_is_applied_to_the_http_client():
    """APP_PROVIDER_TIMEOUT_SECONDS must bound real provider calls."""
    provider = OpenAIResponsesProvider(
        api_key=SecretStr("k"),
        base_url="https://example.invalid/v1",
        default_model="m",
        timeout_seconds=2.0,
    )
    assert provider._timeout.read == 2.0


def test_every_declared_setting_has_a_consumer():
    """Guards against shipping configuration that silently does nothing."""
    fields = set(Settings.model_fields) - {"log_level"}
    source = "\n".join(p.read_text() for p in Path("src").rglob("*.py") if p.name != "settings.py")
    unused = [f for f in fields if f not in source]
    assert unused == [], f"settings never read by the runtime: {unused}"
```

---
### SEC-009 — OpenAPI schema and Swagger/ReDoc UI served without authentication

**Severity:** Low | **Confidence:** High | **Status:** Confirmed
**File:** `src/agent_runtime/api/main.py:203, 236`
**CWE:** CWE-200 (Exposure of Sensitive Information) | **OWASP:** API9:2023 Improper Inventory Management; A05:2025 | **ASVS:** V14.3.2
**CVSS v4.0:** `CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:L/VI:N/VA:N/SC:N/SI:N/SA:N`

**Description.** The security middleware guards only paths beginning with `/v1/`:

```python
# src/agent_runtime/api/main.py:236
if not request.url.path.startswith("/v1/"):
    return await call_next(request)
```

FastAPI's default documentation routes (`/docs`, `/redoc`, `/openapi.json`) fall outside that prefix,
so they are served to anyone who can reach the port, in every auth mode.

**Evidence (experiment V10):**

```
  /healthz         -> 200
  /openapi.json    -> 200 (8 paths described)
  /docs            -> 200
  /redoc           -> 200
```

**Attack scenario.** An unauthenticated attacker retrieves a complete, machine-readable map of the
API: every route, every parameter, the `Idempotency-Key` regex, the policy field names, and the exact
error codes. This is reconnaissance rather than compromise, but it removes all guesswork from
exploiting SEC-001 through SEC-005 — in particular it reveals that `/v1/evaluation-regressions` takes
no client identifier.

**Impact.** Confidentiality: Low. No secrets are exposed; the specification describes structure only.
**Likelihood:** High (default-path scanners request `/openapi.json` routinely).

**Remediation.**
1. Disable the docs in production: `FastAPI(..., docs_url=None, redoc_url=None, openapi_url=None)`
   when `auth_mode == "api_key"`, keeping them enabled for local development.
2. If the schema must stay available, move it behind the boundary — serve it under `/v1/openapi.json`
   so the existing middleware covers it, or add an explicit authentication dependency.
3. Change the middleware guard from a prefix denylist to an **allowlist** of unauthenticated paths
   (`{"/healthz"}`), so future routes are protected by default rather than by remembering the prefix.

**Verification.** `curl -i http://api/openapi.json` with no credentials → `404` (disabled) or `401`.
`curl -i http://api/healthz` → still `200`.

**Regression test.**
```python
@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/redoc"])
def test_api_documentation_is_not_anonymous_in_api_key_mode(path):
    """Only an explicit allowlist may bypass the security boundary."""
    assert client.get(path).status_code in (401, 404)
```

---

### SEC-013 — Exception tracebacks are silently discarded by the JSON log formatter

**Severity:** Low | **Confidence:** High | **Status:** Confirmed
**File:** `src/agent_runtime/observability/logging.py:15-30`
**CWE:** CWE-778 (Insufficient Logging) | **OWASP:** A09:2025 | **ASVS:** V7.1.3, V7.2.1
**CVSS v4.0:** `CVSS:4.0/AV:N/AC:H/AT:P/PR:N/UI:N/VC:N/VI:N/VA:N/SC:N/SI:L/SA:N`

**Description.** `JsonFormatter.format` builds its output from an explicit field allowlist and
`record.getMessage()`. It never inspects `record.exc_info` or `record.stack_info`. Because
`configure_structured_logging` **replaces** the root logger's handlers
(`root_logger.handlers = [handler]`), this applies to every library in the process — uvicorn,
SQLAlchemy, aio-pika, httpx.

```python
# src/agent_runtime/observability/logging.py:33-40
def configure_structured_logging(level: str) -> None:
    root_logger = logging.getLogger()
    ...
    root_logger.handlers = [handler]
```

The allowlist is a deliberate and *correct* control — it is why prompts, payloads and secrets provably
cannot reach stdout, and `test_structured_log_allowlists_only_safe_fields` proves it. The problem is
the side effect: when the API's catch-all handler logs `API_UNHANDLED_ERROR`, the record carries only
the exception's **class name**. The file, line, and call stack are gone. So is every framework-level
traceback — including a worker crashing mid-lease, or an `IntegrityError` that is not an idempotency
race.

**Attack scenario.** Not directly exploitable. The security consequence is investigative: during an
incident, responders have a class name and nothing else. An attacker probing for errors leaves a trail
that cannot be analysed, which meaningfully extends dwell time.

**Impact.** Integrity of the observability record: Low. **Likelihood:** Certain (every exception).

**Remediation.**
1. Add sanitized exception context to the formatter — type, message, and the innermost frame's
   `file:line` — without emitting the full traceback to stdout:
   ```python
   if record.exc_info and record.exc_info[0] is not None:
       exc_type, exc_value, tb = record.exc_info
       payload["exception_type"] = exc_type.__name__
       last = traceback.extract_tb(tb)[-1] if tb else None
       if last is not None:
           payload["exception_location"] = f"{Path(last.filename).name}:{last.lineno}"
   ```
   Deliberately exclude `str(exc_value)` unless the exception type is on an allowlist — exception
   messages are exactly where payload data leaks.
2. Rely on traces for detail. The API middleware already calls `span.record_exception(exc)`, so full
   tracebacks *are* captured in OTel. Extend that to the worker, dispatcher and scheduler, which
   currently do not record exceptions on their spans.
3. Do not append handlers blindly: consider preserving non-`JsonFormatter` handlers behind a flag so
   local debugging is not degraded.

**Verification.** Raise a deliberate error in a test endpoint; confirm the JSON log line contains
`exception_type` and `exception_location` but no request payload, and confirm the OTel span carries
the full stack.

**Regression test.**
```python
def test_exception_context_is_logged_without_leaking_payloads():
    """Tracebacks must be diagnosable without putting request data in logs."""
    record = make_log_record(exc_info=make_exception("secret-prompt-value"))
    line = json.loads(JsonFormatter().format(record))
    assert line["exception_type"] == "ValueError"
    assert "exception_location" in line
    assert "secret-prompt-value" not in json.dumps(line)
```

---

### SEC-014 — W3C trace context accepted unvalidated from untrusted request headers

**Severity:** Low | **Confidence:** High | **Status:** Confirmed
**File:** `src/agent_runtime/api/main.py:330-334`; `src/agent_runtime/observability/telemetry.py:65-66`
**CWE:** CWE-20 (Improper Input Validation); CWE-117 (Improper Output Neutralization for Logs) | **OWASP:** A09:2025; A04:2025 | **ASVS:** V7.3.1
**CVSS v4.0:** `CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:N/VI:L/VA:N/SC:N/SI:L/SA:N`

**Description.** The tracing middleware copies `traceparent` and `tracestate` straight from request
headers into the OTel propagator, with no validation, no size limit, and no policy about whether an
external caller may join an internal trace:

```python
# src/agent_runtime/api/main.py:330-337
carrier = {
    header: request.headers[header]
    for header in ("traceparent", "tracestate")
    if header in request.headers
}
with get_tracer().start_as_current_span(
    f"HTTP {request.method}", context=extract_trace_context(carrier)
) as span:
```

The value is then persisted (`inject_trace_context()` writes the *current* context into
`runs.trace_context`) and propagated through the outbox to the worker.

Notably, the **worker** does this correctly — it filters to exactly the two expected keys
(`worker.py:174-182`), and `test_trace_context_is_injected_and_worker_drops_non_trace_fields` proves
it. The API edge, which faces genuinely untrusted input, has no equivalent filter.

**Evidence (experiment V12):** a request carrying an attacker-chosen trace ID and a 200-character
`tracestate` was accepted with `200`.

**Attack scenario.** An attacker sets `traceparent` to a trace ID they choose, causing their requests
to be grafted onto an arbitrary trace in the operator's backend — polluting or poisoning traces that
responders rely on, and potentially making attacker activity appear as a child of legitimate internal
work. `tracestate` permits up to 32 comma-separated members and is stored and forwarded, providing a
low-bandwidth channel for injecting attacker-controlled strings into the telemetry pipeline. The OTel
collector's shipped Compose configuration uses `verbosity: detailed`, so this content is printed
verbatim in collector output.

**Impact.** Integrity of telemetry: Low. No confidentiality impact. **Likelihood:** Low (requires the
attacker to care about trace hygiene).

**Remediation.**
1. Validate `traceparent` against the W3C format before extracting:
   `^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$`, rejecting all-zero trace and span IDs.
2. Cap `tracestate` length (the W3C spec recommends 512 bytes) and drop it if exceeded.
3. Decide the trust policy explicitly. For a public API the safer default is to **start a fresh root
   span** and record the caller's trace ID as a *link* or an attribute (`arr.client.traceparent`)
   rather than continuing their trace. Accept inbound context only from a trusted ingress that strips
   client-supplied trace headers.
4. Change the collector's Compose exporter from `verbosity: detailed` to `normal`.

**Verification.** Send a malformed `traceparent` → confirm a fresh root span is created, not a
continuation. Send a 4 KB `tracestate` → confirm it is dropped and not persisted to
`runs.trace_context`.

**Regression test.**
```python
@pytest.mark.parametrize(
    "traceparent",
    [
        "not-a-traceparent",
        "00-" + "0" * 32 + "-" + "0" * 16 + "-01",
        "00-abc-def-01",
    ],
)
def test_malformed_inbound_trace_context_is_not_trusted(traceparent):
    """The API edge must validate trace context as strictly as the worker does."""
```

---

### SEC-015 — Third-party GitHub Actions pinned to mutable tags; no security gates in CI

**Severity:** Low | **Confidence:** High | **Status:** Confirmed
**File:** `.github/workflows/ci.yml:20-21, 37, 46, 59-60`
**CWE:** CWE-1357 (Reliance on Insufficiently Trustworthy Component); CWE-494 | **OWASP:** A08:2025 Software and Data Integrity Failures | **SLSA:** v1.2 Build L2 gap | **SCVS:** V2, V6
**CVSS v4.0:** `CVSS:4.0/AV:N/AC:H/AT:P/PR:N/UI:N/VC:L/VI:H/VA:L/SC:N/SI:N/SA:N`

**Description.** The workflow's security fundamentals are good — `permissions: contents: read` at the
top level, `pull_request` rather than the dangerous `pull_request_target`, no secrets referenced
anywhere, and a `concurrency` group. Credit where due: this is better-configured than most CI in
comparable projects.

Two gaps remain.

**Mutable action references.** Only one action is SHA-pinned:

```yaml
- uses: astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9 # v9.0.0   ✅
- uses: actions/checkout@v6                                                     ❌
- uses: actions/setup-python@v6                                                 ❌
- uses: azure/setup-helm@v5.0.0                                                 ❌
- uses: hashicorp/setup-terraform@v4                                            ❌
```

Git tags are mutable. If any of these repositories is compromised, or a maintainer account is taken
over, the tag can be repointed and every subsequent CI run executes attacker code with the
workspace's contents. `actions/*` are first-party and lower risk; `azure/setup-helm` and
`hashicorp/setup-terraform` are third-party.

**No security gates.** The pipeline runs Ruff, mypy, pytest (with `--cov-fail-under=60`), a Compose
smoke test, Helm lint and Terraform validate. It performs **no** dependency vulnerability scanning,
**no** secret scanning, **no** SAST, and **no** container image scanning. Every finding in this report
would pass CI today.

**Attack scenario.** A supply-chain attacker compromises one of the unpinned third-party actions and
repoints its tag. On the next push to `main`, the malicious action runs in the workspace. With
`contents: read` and no secrets the immediate blast radius is genuinely small — the main risks are
poisoning build artifacts and exfiltrating source. The risk grows substantially the moment this
workflow gains deployment credentials.

**Impact.** Integrity of the build pipeline: Moderate. **Likelihood:** Low.

**Remediation.**
1. **SHA-pin every action**, following the pattern already used for `setup-uv`:
   ```yaml
   - uses: actions/checkout@08c6903cd8c0fde910a37f88322edcfb5dd907a8 # v5.0.0
   ```
   Enable Dependabot for `github-actions` so pins are updated with reviewable PRs.
2. **Add the missing gates** as a new `security` job:
   ```yaml
   security:
     runs-on: ubuntu-latest
     steps:
       - uses: actions/checkout@<sha>
         with: { fetch-depth: 0 }          # gitleaks needs full history
       - run: uv run pip-audit --strict    # or osv-scanner --lockfile=uv.lock
       - uses: gitleaks/gitleaks-action@<sha>
       - uses: returntocorp/semgrep-action@<sha>
         with: { config: "p/python p/security-audit p/secrets" }
       - uses: aquasecurity/trivy-action@<sha>
         with: { scan-type: fs, severity: "CRITICAL,HIGH", exit-code: "1" }
   ```
3. **Raise the coverage floor.** 60% is low for security-relevant code; the suite already achieves
   more. Consider a per-package floor for `src/agent_runtime/security` and `domain`.
4. **Generate an SBOM and attach provenance.** `uv export` plus `syft`/`cyclonedx-py` produces a
   CycloneDX SBOM; `actions/attest-build-provenance` provides SLSA provenance for the container image.
5. **Add branch protection** requiring these checks. Not visible in-repo; confirm with the repository
   owner.

**Verification.** Confirm every `uses:` line references a 40-character SHA; confirm the `security` job
fails the build on an intentionally introduced vulnerable dependency and on a planted test secret.

**Regression test.** Add a workflow-lint step (`zizmor` or a small `grep`) asserting no `uses:` line
references a tag rather than a SHA.

---

### SEC-016 — Container image built from a mutable base tag; no digest pinning or SBOM

**Severity:** Low | **Confidence:** High | **Status:** Confirmed
**File:** `docker/Dockerfile:1, 12`; `charts/.../values.yaml:1-4`
**CWE:** CWE-1104 (Use of Unmaintained Third Party Components); CWE-494 | **OWASP:** A08:2025 | **SLSA:** v1.2 provenance gap
**CVSS v4.0:** `CVSS:4.0/AV:N/AC:H/AT:P/PR:N/UI:N/VC:L/VI:L/VA:N/SC:N/SI:N/SA:N`

**Description.** The image is otherwise well built — multi-stage dependency caching, `--frozen`
lockfile installation, `--no-dev` so test dependencies (including the vulnerable `pytest`, SEC-023) are
excluded from the runtime image, a dedicated system user with `/usr/sbin/nologin`, and ownership
correctly transferred before `USER runtime`. These are good practices and should be preserved.

The gaps:

```dockerfile
FROM python:3.12-slim                                   # mutable tag, rebuilt frequently
COPY --from=ghcr.io/astral-sh/uv:0.6.3 /uv /uvx /bin/   # version tag, still mutable
```

- No digest pinning, so two builds of the same commit can produce different base layers.
- No `HEALTHCHECK`.
- `uv` 0.6.3 is pinned by version but the local toolchain is 0.11.17 — a large drift worth reviewing.
- The chart defaults `image.tag` to `.Chart.AppVersion` (`0.1.0`) with `pullPolicy: IfNotPresent`, so
  deployments track a mutable tag rather than an immutable digest.
- No SBOM is generated at build time.

**Attack scenario.** A compromised or simply-changed upstream base image is pulled into a rebuild
without any reviewable change to this repository. Because the chart also references a mutable tag, a
compromised registry tag can be deployed without a Helm change.

**Impact.** Integrity: Low. **Likelihood:** Low.

**Remediation.**
1. Pin the base by digest and keep the tag as a comment:
   `FROM python:3.12-slim@sha256:<digest>  # 3.12-slim as of YYYY-MM-DD`
2. Pin the `uv` copy stage by digest; review the 0.6.3 → 0.11.x upgrade.
3. Deploy by digest: set `image.tag` to `@sha256:…` (or add an `image.digest` value) and use
   `pullPolicy: Always` for mutable tags.
4. Add a `HEALTHCHECK` for non-Kubernetes runtimes (Kubernetes uses the chart's probes).
5. Generate a CycloneDX SBOM in CI and publish it as a release artifact (SCVS V1/V2).
6. Add `trivy image --severity HIGH,CRITICAL --exit-code 1` to CI (see SEC-015).
7. Enable Dependabot/Renovate for both the Dockerfile and `uv.lock`.

**Verification.** `docker inspect <image> | jq '.[0].RootFS.Layers'` reproducible across builds of the
same commit; `trivy image` reports zero HIGH/CRITICAL; the SBOM lists all 78 packages.

---

### SEC-017 — `X-Client-Id` length is never checked against its `VARCHAR(128)` column

**Severity:** Low | **Confidence:** Medium | **Status:** Likely — confirmed in code and in the audit path; the `runs`-table path was not exercised against a live PostgreSQL instance
**File:** `src/agent_runtime/api/main.py:627-634`; `src/agent_runtime/infrastructure/database/models.py:49, 204`
**CWE:** CWE-20 (Improper Input Validation); CWE-1284 | **OWASP:** A03:2025; API8:2023 | **ASVS:** V5.1.3
**CVSS v4.0:** `CVSS:4.0/AV:N/AC:L/AT:N/PR:L/UI:N/VC:N/VI:N/VA:L/SC:N/SI:N/SA:N`

**Description.** `_required_client_id()` enforces only non-emptiness. Both `runs.client_id` and
`security_audit_events.client_id` are `String(128)`. PostgreSQL rejects an over-length value with
`value too long for type character varying(128)`, raising `DataError` — which is not among the
exceptions `create_run` handles, so it reaches the catch-all handler and returns `500 INTERNAL_ERROR`.

This is the same root cause as SEC-007 (which covers the audit-suppression impact); listed separately
because the API-error impact is distinct and the fix is shared.

Contrast with `Idempotency-Key`, which *is* strictly validated (`IDEMPOTENCY_KEY_PATTERN`, 8–255
URL-safe characters) — the pattern to follow already exists in the file.

**Impact.** Availability: Low (a `500` per request, not a crash). Also a minor information-disclosure
consideration: `500` versus `422` distinguishes a validation boundary. **Likelihood:** Medium.

**Remediation.** Apply a strict pattern mirroring the idempotency key:

```python
CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _required_client_id(client_id: str | None) -> str:
    if client_id is None or not CLIENT_ID_PATTERN.fullmatch(client_id.strip()):
        raise ApiProblem(status_code=422, code="INVALID_CLIENT_ID", message="...")
    return client_id.strip()
```

After SEC-001 this validation moves to credential registration, where it belongs — but the boundary
check should remain as defence in depth. Additionally, add `DataError`/`IntegrityError` handling to
`create_run` so a constraint violation returns `422`, never `500`.

**Verification.** `X-Client-Id` of 129 characters → `422`, not `500`. With control characters or
whitespace → `422`. Exactly 128 valid characters → accepted.

**Regression test.**
```python
@pytest.mark.parametrize(
    "client_id,expected",
    [
        ("a" * 128, 202),
        ("a" * 129, 422),
        ("", 422),
        ("has space", 422),
        ("null\x00byte", 422),
    ],
)
def test_client_id_is_validated_against_its_column_width(client_id, expected):
    """An over-length header must be a 422, never a 500 or a suppressed audit write."""
```

---

### SEC-018 — The application role owns its own schema and runs migrations

**Severity:** Low | **Confidence:** High | **Status:** Confirmed
**File:** `charts/.../templates/migration-job.yaml:31-33`; `migrations/env.py:18-19`; `migrations/versions/20260906_01_*.py:151-173`
**CWE:** CWE-250 (Execution with Unnecessary Privileges); CWE-269 | **OWASP:** A01:2025 (least privilege); A05:2025 | **ASVS:** V14.1.3
**CVSS v4.0:** `CVSS:4.0/AV:L/AC:L/AT:P/PR:H/UI:N/VC:L/VI:H/VA:L/SC:N/SI:N/SA:N`

**Description.** The migration Job and all four runtime processes read the same `APP_DATABASE_URL`
from the same Secret. The migration credential must hold DDL rights — `CREATE TABLE`, `CREATE
FUNCTION`, `CREATE TRIGGER` — so every long-running process inherits them.

The consequence undermines an otherwise excellent control. Migrations 01 and 06 install triggers that
make `run_events`, `run_attempts` and `security_audit_events` append-only:

```sql
CREATE FUNCTION prevent_security_audit_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'security_audit_events are append-only';
END;
$$ LANGUAGE plpgsql;
```

This is genuinely good design — durable, database-enforced, not application-enforced. But because the
API's own role owns those triggers, an attacker with code execution in the API pod can issue
`ALTER TABLE security_audit_events DISABLE TRIGGER ALL`, rewrite or delete the audit history, and
re-enable it. The tamper-evidence the trigger provides holds against application bugs, not against a
compromised application.

**Impact.** Integrity: Moderate, post-compromise. **Likelihood:** Low (requires prior compromise).

**Remediation.**
1. **Separate the roles.** Create `arr_migrator` (owns the schema, DDL) and `arr_runtime`
   (`SELECT, INSERT, UPDATE` on the runtime tables; `INSERT` only on `run_events`,
   `run_attempts` and `security_audit_events`; **no** `DELETE`, **no** `ALTER`, **no** ownership).
2. Give the migration Job its own Secret key (`APP_MIGRATION_DATABASE_URL`) — this pairs naturally
   with the Secret split in SEC-012.
3. Add table-level grants to the migration that installs the triggers, so the least-privilege posture
   is version-controlled rather than a runbook step.
4. Consider shipping audit records to an append-only store outside the application's blast radius
   (a SIEM, or an object store with object-lock) for true tamper-evidence.

**Verification.** Connect as `arr_runtime` and attempt
`ALTER TABLE security_audit_events DISABLE TRIGGER ALL` → expect `permission denied`. Attempt
`DELETE FROM run_events` → expect `permission denied`. Confirm the application's normal write paths
still succeed.

---

### SEC-019 — Terraform namespaces lack Pod Security Admission labels and NetworkPolicy

**Severity:** Low | **Confidence:** High | **Status:** Confirmed
**File:** `infra/terraform/modules/runtime_namespace/main.tf:1-32`
**CWE:** CWE-1008 (Architectural Concept Violation — missing defence in depth); CWE-923 | **OWASP:** A05:2025 | **ASVS:** V14.1.1
**CVSS v4.0:** `CVSS:4.0/AV:A/AC:L/AT:P/PR:L/UI:N/VC:L/VI:L/VA:L/SC:N/SI:N/SA:N`

**Description.** The Terraform baseline is deliberately minimal and that restraint is well-reasoned —
`infra/terraform/README.md` explains why it does not create clusters or managed datastores without an
account-owner decision, and the state/secret handling is correct (HTTP backend, no secret values in
state, `*.tfvars` gitignored with an `!*.tfvars.example` exception). The module creates a namespace
and a `ResourceQuota`, and the quota is a genuine availability control.

What is missing, given that this module defines the namespace's security posture:

1. **No Pod Security Admission labels.** Adding them would enforce SEC-011's controls at the
   namespace level, so a future workload cannot opt out.
2. **No default-deny NetworkPolicy.** Any pod in the cluster can reach the API Service and the
   datastores. There is nothing restricting egress from the pods either.
3. **No `LimitRange`.** The `ResourceQuota` caps the namespace total but nothing prevents a single pod
   from claiming it.

**Impact.** Confidentiality/Integrity/Availability: Low each, as defence-in-depth gaps.
**Likelihood:** Low.

**Remediation.**
```hcl
resource "kubernetes_namespace_v1" "runtime" {
  metadata {
    name = var.namespace
    labels = merge({
      "pod-security.kubernetes.io/enforce"         = "restricted"
      "pod-security.kubernetes.io/enforce-version" = "latest"
      "pod-security.kubernetes.io/audit"           = "restricted"
      "pod-security.kubernetes.io/warn"            = "restricted"
    }, local.base_labels, var.labels)
  }
}

resource "kubernetes_network_policy_v1" "default_deny" {
  metadata {
    name      = "default-deny-ingress"
    namespace = kubernetes_namespace_v1.runtime.metadata[0].name
  }
  spec {
    pod_selector {}
    policy_types = ["Ingress"]
  }
}
```

Then add explicit allow policies: ingress to the API from the ingress controller only; egress from the
pods to PostgreSQL, Redis, RabbitMQ, the OTel collector and (worker only) the provider endpoint. Add a
`LimitRange` with per-container defaults. Note that `enforce: restricted` will **reject** the current
pod specs until SEC-011 is fixed — sequence SEC-011 first, then adopt PSA to lock it in. Add
`checkov` or `tfsec` to the `terraform-validate` CI job.

**Verification.** `kubectl get ns arr-production -o jsonpath='{.metadata.labels}'` shows the PSA
labels. Deploying a privileged pod into the namespace is rejected. A test pod in another namespace
cannot reach the API Service.

---

### SEC-022 — Caller input is echoed into `result_payload`; no retention or encryption policy

**Severity:** Low | **Confidence:** High | **Status:** Confirmed
**File:** `src/agent_runtime/providers/deterministic.py:13-19`; `src/agent_runtime/infrastructure/database/models.py:52-53, 65`
**CWE:** CWE-359 (Exposure of Private Personal Information); CWE-311 (Missing Encryption of Sensitive Data) | **OWASP:** A02:2025; A04:2025 | **ASVS:** V6.1.1, V8.1.1 (data protection, retention)
**CVSS v4.0:** `CVSS:4.0/AV:L/AC:L/AT:P/PR:H/UI:N/VC:H/VI:N/VA:N/SC:N/SI:N/SA:N`

**Description.** The default provider reflects the caller's entire input and policy back as the run
result, which is then persisted:

```python
# src/agent_runtime/providers/deterministic.py:16-19
return ExecutionResult(
    provider=self.name,
    result_payload={"accepted_input": input_payload, "policy_applied": policy_snapshot},
)
```

Three related observations:

1. **Duplication.** With the default provider, caller input is stored twice — in
   `runs.input_payload` and again in `runs.result_payload`. This is reasonable for a deterministic
   test double, but it is also the *shipped default*, so real deployments that have not configured a
   provider store every prompt twice.
2. **No encryption at rest.** Both columns are plain JSONB. Prompts are frequently the most sensitive
   data an LLM system handles and may contain PII. Protection depends entirely on the database's own
   disk encryption, which is outside this repository.
3. **No retention policy.** Nothing expires, archives, or deletes run data. `run_events` and
   `run_attempts` are append-only by trigger and `runs` is referenced by `ON DELETE RESTRICT` foreign
   keys, so there is no deletion path at all — which makes a GDPR/CCPA erasure request
   architecturally difficult to satisfy.

To be clear about what is already right: prompts are correctly excluded from logs (allowlist), from
metrics (cardinality control), from traces (no payload capture), and from outbox messages (minimal
payload). The gap is specifically at rest.

**Impact.** Confidentiality: Moderate given a database compromise or backup exposure.
**Likelihood:** Low (requires prior access), but the **compliance** exposure is present continuously.

**Remediation.**
1. Make the deterministic provider's echo opt-in (`policy.echo_input: true`) or return only a digest
   of the input by default, so prompt data is not duplicated as a side effect of the default config.
2. Add application-level encryption for `input_payload` and `result_payload` using an
   envelope-encryption scheme with a KMS-managed key, or document a hard requirement for
   transparent data encryption plus encrypted backups.
3. Define a retention policy: a `retention_expires_at` column, a scheduled purge process, and a
   documented default window.
4. Provide a deletion path for erasure requests. Given the append-only triggers, the cleanest approach
   is **crypto-shredding** — destroy the per-tenant data key so the ciphertext becomes unreadable
   while the immutable audit structure stays intact. This preserves both properties, which a `DELETE`
   cannot.
5. Document in the README what the runtime stores, for how long, and how to request deletion.

**Verification.** Confirm the default provider no longer duplicates input. Confirm `psql` on a stored
run returns ciphertext rather than plaintext prompts. Confirm the purge job removes data past the
retention window and that crypto-shredding renders a tenant's data unreadable.

---

### SEC-021 — Local Compose stack ships weak credentials and exposes datastore ports

**Severity:** Informational | **Confidence:** High | **Status:** Confirmed — **development-only, and documented as such**
**File:** `docker-compose.yml:8-10, 68-131`
**CWE:** CWE-1188; CWE-798 (Use of Hard-coded Credentials) | **OWASP:** A05:2025 | **ASVS:** V14.1.1

**Description.** The Compose stack uses `runtime:runtime` for PostgreSQL and RabbitMQ, runs Redis with
no password, ships Grafana with its default `admin:admin` credentials, and binds 5432, 6379, 5672,
15672, 9090, 3000, 4318 and 8889 to the host. `APP_AUTH_MODE` is `disabled`.

**This is reported as Informational rather than as a vulnerability**, because it is an explicit,
documented, and defensible choice. `SECURITY.md` states: *"Local Compose is a development demo and
intentionally uses disabled authentication."* The README frames the same stack as a credentials-free
five-minute demo, which is a legitimate and valuable goal. The chart, which defines the production
path, correctly defaults to `authMode: api_key` and exposes only a ClusterIP.

The residual risk is developer-machine exposure: a laptop on a shared or café network running
`make dev` exposes PostgreSQL and an unauthenticated API to that network.

**Remediation (hardening, not a defect fix).**
1. Bind host ports to loopback: `"127.0.0.1:5432:5432"` and likewise for 6379, 5672, 15672, 8000.
   This preserves the entire developer experience while removing network exposure — the single
   highest-value change here.
2. Remove the `15672` (RabbitMQ management) and `8889` publications unless actively used.
3. Set `GF_SECURITY_ADMIN_PASSWORD` and `GF_AUTH_ANONYMOUS_ENABLED=false` on the Grafana service.
4. Add a one-line comment at the top of `docker-compose.yml` stating it is development-only, so the
   warning travels with the file rather than only in `SECURITY.md`.

---

### SEC-023 — `pytest` 8.4.2 carries PYSEC-2026-1845 (dev-only, absent from the runtime image)

**Severity:** Informational | **Confidence:** High | **Status:** Confirmed — **not reachable in production**
**File:** `uv.lock` (pytest 8.4.2, dev dependency group)
**CWE:** CWE-379 (Creation of Temporary File in Directory with Insecure Permissions) | **CVE:** CVE-2025-71176 | **GHSA:** GHSA-6w46-j5rx-g56g | **OWASP:** A06:2025 Vulnerable and Outdated Components

**Description.** OSV reports: *"pytest through 9.0.2 on UNIX relies on directories with the
`/tmp/pytest-of-{user}` name pattern, which allows local users to cause a denial of service or
possibly gain privileges."* Fixed in pytest 9.0.3.

**Reachability analysis — the reason this is Informational, not Low:**

1. **Is the vulnerable version used?** Yes — `uv.lock` pins 8.4.2.
2. **Is the vulnerable functionality reachable?** Only via `tmp_path`/`tmpdir` fixtures during test
   execution. Exploitation requires a **local** unprivileged user on the machine running pytest to win
   a race on a predictable `/tmp` path.
3. **Is it in the production image?** **No.** The Dockerfile runs `uv sync --frozen --no-dev`, so the
   entire dev group is excluded. Verified in `docker/Dockerfile:14, 20`.
4. **Direct or transitive?** Direct dev dependency.
5. **Realistic exploitability?** Very low. It requires a hostile local user on a developer workstation
   or CI runner. GitHub-hosted runners are ephemeral and single-tenant.

**This is the entire vulnerability surface of the dependency tree.** A full OSV batch query across all
77 third-party locked packages returned exactly this one advisory.

**Remediation.** Upgrade when convenient — it is a dev-tooling hygiene item, not a production risk:

```bash
uv add --dev 'pytest>=9.0.3,<10'
uv lock && uv sync --dev && uv run pytest
```

Note that pytest 9 is a major version; verify `pytest-asyncio` 0.26 and `pytest-cov` 7.1 compatibility.
If that upgrade is disruptive, deferring is a defensible decision — document it and revisit.

**Verification.** `uvx --from pip-audit pip-audit --path .venv/lib/python3.13/site-packages` reports
zero vulnerabilities.

---
## 10. Authentication Review

**Verdict: the mechanism is correct; the model is incomplete.**

| Control | Status | Notes |
| ------- | ------ | ----- |
| Password hashing / storage | N/A | No passwords; API-key model only |
| Credential comparison | **PASS** | `hmac.compare_digest` — correct, verified constant-time (V17) |
| Raw credential handling | **PASS** | Never stored, logged, traced, or persisted; proven by `test_..._without_retaining_raw_value` |
| Credential storage strength | **FAIL** | Unsalted single-round SHA-256 — SEC-010 |
| Credential → identity binding | **FAIL** | Authentication yields no principal — SEC-001 |
| Multi-tenant credentials | **FAIL** | Exactly one key for the whole deployment |
| Key rotation | **FAIL** | No mechanism; explicitly deferred by ADR-0004 |
| Key revocation | **FAIL** | Requires a config change and restart |
| Minimum credential entropy | **FAIL** | Not required, not documented, not validated |
| Brute-force protection | **PARTIAL** | The Redis limiter applies — but only when `X-Client-Id` is sent (SEC-006), and it is bypassable |
| Account enumeration | N/A | No accounts exist |
| Authentication error messages | **PASS** | `401` for missing, `403` for invalid, with generic text — no oracle |
| MFA | N/A | Out of scope for a service-to-service V0 |
| JWT | N/A | **Not used.** No JWT anywhere — algorithm confusion, `alg: none`, and claim-manipulation risks are all inapplicable |
| OAuth / OIDC / PKCE | N/A | Not implemented |
| Sessions / cookies | N/A | Stateless; no cookies are set or read anywhere in the codebase |
| Logout / session invalidation | N/A | Stateless |
| Bearer token extraction | **PASS** | Scheme compared case-insensitively; empty credentials rejected (`authentication.py:44-47`) |
| Auth enabled by default | **FAIL** | Defaults to `disabled` — SEC-008 |
| Unauthenticated surface | **PARTIAL** | `/healthz` intended; `/docs`, `/redoc`, `/openapi.json` unintended — SEC-009 |

**Assessment.** `authenticate_api_key()` is a well-written 48-line function: it fails closed, uses a
constant-time comparison, never retains the raw key, raises rather than silently passing when
configuration is inconsistent, and separates "missing" from "invalid" correctly. Reviewed purely as a
credential check, it is sound.

The limitation is architectural: the function answers *"is this key valid?"* and nothing more. In a
single-tenant internal service that is sufficient. In a system whose data model is explicitly
multi-tenant — `runs.client_id`, per-client idempotency, per-client rate limiting — an authentication
layer that produces no principal leaves the authorization layer with nothing to enforce. That gap is
SEC-001, and it is the single most important thing to fix in this repository.

---

## 11. Authorization & Access Control Review

**Verdict: FAIL. This is the weakest area of the system.**

### Access-control model

There is no RBAC, no ABAC, no policy engine, and no privilege tier. There are exactly two states:
"has the key" and "does not." Every authorization decision reduces to an ownership filter on a
caller-supplied string.

### IDOR / BOLA assessment

| Endpoint | Object | Ownership check | Result |
| -------- | ------ | --------------- | ------ |
| `GET /v1/runs/{id}` | Run | `WHERE id = :id AND client_id = :client_id` | **Bypassable** — `client_id` is a header |
| `GET /v1/runs/{id}/attempts` | Attempts | Parent run checked first | **Bypassable** |
| `GET /v1/runs/{id}/events` | Events | Parent run checked first | **Bypassable** |
| `GET /v1/runs/{id}/evaluations` | Evaluations | Parent run checked first | **Bypassable** |
| `POST /v1/runs/{id}/evaluations` | Run (write) | Parent run checked first | **Bypassable** |
| `POST /v1/runs/{id}/replay` | Run (write) | Parent run checked first | **Bypassable** |
| `POST /v1/evaluation-regressions` | — | **None at all** | SEC-003 |

The *structure* of the ownership check is right. `_get_run_for_client` correctly filters at the query
level rather than fetching-then-comparing, returns `RunNotFoundError` (a `404`) rather than a `403` so
existence is not disclosed, and is consistently applied to every child resource through its parent.
This is textbook. The value it filters on is simply not trustworthy.

Run IDs are UUIDv4 — not enumerable, which is a meaningful mitigating control. But UUIDs are
identifiers, not capabilities: they appear in API responses, logs, trace attributes, and the demo
script's stdout. The design must not depend on their secrecy.

### Broken Function Level Authorization

**Not applicable in the conventional sense** — there are no administrative endpoints to reach. This is
worth noting as a positive: the API surface is small and uniform, with no hidden management routes,
debug endpoints, or internal-only paths that could be reached by a normal caller. The absence of an
admin tier is also why no vertical privilege escalation was found.

### Broken Object Property Level Authorization (mass assignment)

| Vector | Result |
| ------ | ------ |
| `CreateRunRequest` top level | **PASS** — `extra="forbid"` |
| `EvaluateRunRequest` | **PASS** — `extra="forbid"` |
| All regression request models | **PASS** — `extra="forbid"` |
| **`policy` sub-object** | **FAIL** — typed `dict[str, JsonValue]`, unvalidated; arbitrary keys persist into the immutable snapshot, and `max_attempts`/`attempt_timeout_seconds`/`max_output_tokens`/`model`/`instructions` all reach behaviour-changing sinks — SEC-004 |
| `input` sub-object | Acceptable — opaque by design, size-capped |
| Server-controlled response fields | **PASS** — `run_id`, statuses, timestamps and `routing_decision` are all server-generated; no client-settable status field exists |

There is no `isAdmin`-style field to inject, because no privilege field exists. The mass-assignment
exposure is confined to `policy`, and it is real: `extra="forbid"` is applied at the top level but not
one level down.

### Other authorization observations

- **Routing metrics are catalog-owned, not caller-supplied.** `resolve_routing_decision` reads from
  `DEFAULT_PROVIDER_CATALOG` and rejects unknown providers; a caller can choose *among* candidates but
  cannot inject latency/cost/quality figures to influence selection. The docstring states this intent
  explicitly. **This is a genuinely good design decision** and closes what would otherwise be an
  obvious manipulation vector.
- **Provider availability is enforced server-side** — `available_providers` is derived from whether
  `APP_OPENAI_API_KEY` is configured, so a caller cannot route to an unconfigured provider.
- **No frontend-only authorization exists**, because there is no frontend. Every check is server-side.
- **Worker-side authorization is sound** — the lease model (`_owns_active_lease`) correctly requires
  matching `worker_id` *and* an unexpired lease before any terminal write, preventing one worker from
  completing another's attempt.

---

## 12. API Security Review (OWASP API Security Top 10:2023)

| # | Category | Status | Evidence |
| - | -------- | ------ | -------- |
| API1 | Broken Object Level Authorization | **FAIL** | SEC-001 — object ownership keyed on a caller-supplied header |
| API2 | Broken Authentication | **PARTIAL** | Mechanism correct (constant-time, no raw retention); model incomplete (single key, no rotation, weak KDF, fail-open default) — SEC-008, SEC-010 |
| API3 | Broken Object Property Level Authorization | **FAIL** | SEC-004 — `policy` accepts arbitrary keys and unbounded values |
| API4 | Unrestricted Resource Consumption | **FAIL** | SEC-002, SEC-003, SEC-004, SEC-005, SEC-006 — the dominant weakness class |
| API5 | Broken Function Level Authorization | **PASS** | No administrative tier exists; no route is reachable that should not be |
| API6 | Unrestricted Access to Sensitive Business Flows | **FAIL** | SEC-003 — paid inference reachable without identity or anti-automation |
| API7 | Server Side Request Forgery | **PASS** | No caller-controlled URL exists anywhere; `openai_base_url` is operator-only. Verified — see §25 |
| API8 | Security Misconfiguration | **PARTIAL** | SEC-008, SEC-009, SEC-011, SEC-012, SEC-019, SEC-021 |
| API9 | Improper Inventory Management | **PARTIAL** | SEC-009 (schema public); positives below |
| API10 | Unsafe Consumption of APIs | **PASS** | Best-in-class here — see below |

### API4 detail — resource-consumption controls

| Control | Present | Notes |
| ------- | ------- | ----- |
| Pagination on list endpoints | **No** | `/attempts`, `/events`, `/evaluations` return unbounded lists. A run with millions of attempts (SEC-004) makes these responses enormous |
| Request body size limit | Partial | Header-based only — SEC-005 |
| `input` size limit | **Yes** | 64 KiB, enforced in the model |
| Array length limits | Partial | `rules` ≤ 32, `cases` ≤ 100 — but per-element size is unbounded |
| Per-element content limits | **No** | A single rule or case may be arbitrarily large |
| Rate limiting | Partial | Bypassable — SEC-006; absent on the costliest endpoint — SEC-003 |
| Request timeout | **No** | No server-side deadline on any handler |
| Provider call budget | **No** | No spend cap, no per-client quota |
| Concurrent-run quota | **No** | A single client may queue unlimited work |
| Provider timeout | **No** | The setting exists but is never read — SEC-020 |
| CPU bound on evaluation | **No** | SEC-002 |

**Add pagination** to the three history endpoints as part of the SEC-004 remediation; they are the
read-side expression of the same unbounded-growth problem.

### API10 — Unsafe Consumption of APIs: a model implementation

The OpenAI adapter is the strongest security code in the repository and deserves explicit credit:

- Every field of the provider response is individually type-guarded (`_string_or_none`,
  `_usage_metadata`, `_output_text`) — a malicious or malformed upstream response cannot inject
  unexpected types into the persisted result.
- `store: false` is sent, so prompts are not retained by the provider.
- Provider **error bodies are never persisted** — `ProviderHttpError` carries only the status code
  (`contracts.py:24-29`), eliminating a common leak path for upstream secrets and internal details.
- Only `id`, `model`, `output_text` and token counts are stored; nothing else from the response.
- Status codes are mapped to a closed set of internal error codes, so upstream text never reaches the
  retry classifier or the metrics labels.
- The base URL is operator-controlled, not caller-controlled.

The one gap is the unwired timeout (SEC-020).

### API9 — Inventory positives

The API is genuinely well-inventoried: a single `/v1` version, no deprecated routes, no undocumented
endpoints (all 9 routes appear in the OpenAPI schema and in the README), and the one legacy artifact —
the ARR-6 RabbitMQ queue — is explicitly documented, consumed-but-not-bound, and commented in code.
That is better inventory discipline than most production APIs. The only issue is that the schema is
served anonymously (SEC-009).

---

## 13. Input Validation & Injection Review

| Class | Status | Evidence |
| ----- | ------ | -------- |
| SQL Injection | **PASS** | No raw SQL. `sa.text` is never imported in `src/`. All queries use SQLAlchemy Core/ORM expression constructs with bound parameters. No dynamic query or filter construction anywhere |
| NoSQL Injection | N/A | No NoSQL datastore |
| OS Command Injection | **PASS** | No `subprocess`, `os.system`, `os.popen`, or `shell=True` in `src/` |
| Code Injection | **PASS** | No `eval`, `exec`, `compile`, or `__import__` |
| **Regex injection / ReDoS** | **FAIL** | **SEC-002** — caller-supplied regex executed unbounded. The most significant injection-class finding |
| JSON Schema injection | **FAIL** | SEC-002 — caller-supplied schema, including `pattern` |
| LDAP / XPath Injection | N/A | Neither is used |
| Server-Side Template Injection | **PASS** | No template engine in the application. Helm templating is build-time and operator-controlled |
| XSS (stored/reflected/DOM) | N/A | JSON-only API; no HTML rendering, no `text/html` responses except the FastAPI docs pages |
| HTML injection | N/A | As above |
| CRLF injection / response splitting | **PASS** | Headers are set only from server-controlled values (`Retry-After` from an int) |
| XXE | **PASS** | No XML parsing anywhere |
| Unsafe deserialization | **PASS** | Only `json.loads`; no `pickle`, `marshal`, `shelve`, or `yaml.load` |
| Path traversal | **PASS** | Filesystem access only in the operator-run CLI (`Path(args.dataset)`), not reachable from the API |
| Open redirect | N/A | No redirects issued |
| SSRF | **PASS** | No caller-controlled URL — see §25 for the tested-and-refuted `$ref` hypothesis |
| Prototype pollution | N/A | Python, not JavaScript |
| Mass assignment | **FAIL** | SEC-004 — `policy` sub-object |
| Integer/quantity validation | **FAIL** | SEC-004 — sign and type checked, magnitude not |
| Header validation | **PARTIAL** | `Idempotency-Key` strictly validated; `X-Client-Id` and `traceparent` not — SEC-014, SEC-017 |
| Message-queue input | **PASS** | Strict JSON + UUID validation; trace keys filtered to an allowlist; malformed messages rejected without requeue to a poison queue |

**Assessment.** For the classic injection families this codebase is genuinely clean, and not by
accident — the ORM is used idiomatically throughout, the AMQP consumer validates defensively, and the
domain layer has a test (`test_domain_does_not_depend_on_delivery_frameworks`) enforcing the
architectural boundary.

The failures are all in one place: the evaluation rule language. The system accepts caller-authored
*executable expressions* — regular expressions and JSON Schemas — and treats them as data. They are
not data; they are programs, with their own resource-consumption semantics. That is the injection
story in this repository, and it is a real one.

---

## 14. Business Logic Security Review

This section received particular attention, since automated tooling finds none of it.

| Abuse pattern | Status | Notes |
| ------------- | ------ | ----- |
| State transition bypass | **PASS** | `ALLOWED_EXECUTION_TRANSITIONS` is explicit and exhaustively tested; terminal states have empty transition sets; `claim()` re-checks status under a row lock |
| Workflow bypass | **PASS** | Evaluation requires `SUCCEEDED` **and** a non-null `result_payload` (`run_service.py:168`); replay requires an existing owned run |
| Approval bypass | N/A | No approval workflow exists |
| Duplicate transaction | **PASS** | Unique constraint on `(client_id, idempotency_key)` is the final authority; `IntegrityError` is caught and re-resolved |
| Replay attack (protocol) | **PASS** | `canonical_request_hash` sorts keys, so semantically identical bodies match regardless of ordering; a mismatched body on a reused key returns `409` |
| Race condition — idempotency | **PASS** | `test_concurrent_identical_submissions_create_one_run` proves the DB constraint wins the race |
| Race condition — worker claim | **PASS** | `SELECT … FOR UPDATE` plus a lease; duplicate delivery is explicitly handled and acknowledged without a second execution |
| Race condition — lease recovery | **PASS** | `FOR UPDATE SKIP LOCKED` prevents two schedulers colliding |
| Race condition — outbox | **PASS** | `FOR UPDATE SKIP LOCKED` with `published_at IS NULL` |
| Double submission | **PASS** | Idempotency key required on both `POST /v1/runs` and replay |
| Optimistic concurrency | **PASS** | `version_id_col` on `Run` |
| Price / quantity manipulation | Partial | No pricing logic exists, but `estimated_cost_microusd` in a regression request is caller-supplied and merely reported — it does not drive any decision, so this is cosmetic rather than exploitable |
| Client-controlled calculations | **PASS** | Routing scores are computed server-side from a server-owned catalog |
| **Client-controlled limits** | **FAIL** | **SEC-004** — retry bounds are caller-authored |
| **Unbounded resource creation** | **FAIL** | SEC-004 (retry storm), SEC-003 (provider calls). No per-client run quota |
| Privilege transition errors | N/A | No privilege tiers |
| Coupon / credit abuse | N/A | No such feature |
| Inconsistent validation | **FAIL** | Settings bounds apply to defaults but not to caller values — the same parameter is validated two different ways depending on its origin (SEC-004) |
| Multi-step workflow bypass | **PASS** | The two-phase evaluation lifecycle (PENDING → terminal) re-verifies ownership in the second transaction (`run_service.py:227`) — a detail that is easy to get wrong and was got right |
| Idempotency scope manipulation | **FAIL** | Scoped by caller-asserted `client_id` — SEC-001/SEC-015 note in §9 |

### Notable positive: the evaluation lifecycle

`SqlAlchemyRunService.evaluate()` splits into three phases — persist `PENDING`, compute outside the
transaction, then persist the outcome — and re-fetches the run *with an ownership check* in the third
phase rather than trusting the first. It also uses `with_for_update=True` on the evaluation row and
verifies `persisted_evaluation.run_id == run.id` before writing. That is careful, defensive
transactional code.

### Notable positive: failure classification

The retry classifier is correctly conservative. `401`/`403` and `4xx` are non-retryable; `429`, `5xx`
and timeouts are retryable. This prevents the classic failure mode where an authentication error
triggers a retry storm against the provider. `test_client_errors_are_not_retryable` locks it in.

### The gap

Every reliability-oriented business rule in this system is well-constructed and well-tested. Every
*abuse*-oriented rule — quotas, budgets, caps, per-tenant fairness — is absent. The system was
designed against failure, not against a hostile caller. That is a coherent V0 scope decision, but it
is the gap that SEC-002 through SEC-006 all occupy.

---

## 15. Secrets & Cryptography Review

### Secret scanning results

| Scope | Result |
| ----- | ------ |
| Source (`src/`, `tests/`, `examples/`) | **Clean.** The only key-like literals are test fixtures (`"sdk-test-key"`, `"integration-test-api-key"`, `"wrong-secret"`) — clearly synthetic, used only to assert redaction behaviour |
| `.env.example` | **Clean.** Placeholders only, with an explicit "Do not place real credentials here" warning and correct guidance to store a digest rather than a raw key |
| `docker-compose.yml` | Development credentials only (`runtime:runtime`) — SEC-021 |
| `docker/`, `charts/`, `infra/` | **Clean.** The chart creates no credential values; Terraform handles secret *names* only and never reads or outputs values |
| `infra/terraform/backend.hcl.example` | Placeholders (`replace-with-state-service-token`) |
| CI workflows | **Clean.** No `secrets.*` reference anywhere |
| **Git history (all 21 commits)** | **Clean.** No live credential was ever committed |
| `.gitignore` | **Good.** Covers `.env`, `*.tfstate`, `*.tfvars` with an `!*.tfvars.example` exception, and `**/.terraform/` |

**No secret rotation is required as a result of this audit.** This is a genuinely clean result and
reflects deliberate discipline.

One note: `infra/terraform/environments/*/.terraform/providers/**` contains checked-in provider
binaries (~100 MB of `terraform-provider-kubernetes_v2.38.0_x5`) despite `**/.terraform/` being
gitignored — they predate the ignore rule or were force-added. These are not secrets, but vendored
binaries in Git are a supply-chain smell; remove them from the working tree.

### Cryptography review

| Item | Status | Notes |
| ---- | ------ | ----- |
| Algorithms in use | Partial | SHA-256 only |
| Deprecated algorithms | **PASS** | No MD5, SHA-1, DES, RC4, or ECB anywhere |
| Custom cryptography | **PASS** | **None.** No hand-rolled crypto — the single most important thing to get right, and it was |
| Password hashing | N/A | No passwords |
| Credential verifier | **FAIL** | Unsalted single-round SHA-256 — SEC-010 |
| Constant-time comparison | **PASS** | `hmac.compare_digest`, verified |
| Hard-coded keys | **PASS** | None |
| Static IV / ECB mode | N/A | No symmetric encryption |
| Randomness | **PASS** | `uuid.uuid4()` uses `os.urandom`. No `random` module use for security purposes |
| Predictable identifiers | **PASS** | UUIDv4 for all resource IDs |
| Key storage | **PASS in code** | `SecretStr` for the API key and OpenAI key prevents accidental repr/log leakage |
| Key storage | **PARTIAL in deployment** | Over-distributed — SEC-012 |
| Integrity protection | N/A | No signed payloads |
| Encryption at rest | **FAIL** | Prompts stored as plaintext JSONB — SEC-022 |
| TLS enforcement | **Not enforced in-app** | `redis://` and `amqp://` are permitted alongside `rediss://`/`amqps://`; the OTel endpoint defaults to plain `http://`. Acceptable if the mesh/network provides TLS, but the application does not require it |
| Hashing for non-security use | **PASS** | `canonical_request_hash` and the rate-limit key both use SHA-256 appropriately — collision resistance is the only property needed |

**Recommendation beyond SEC-010:** require `rediss://`/`amqps://` schemes when `auth_mode == "api_key"`,
validated in `model_post_init`. That converts a deployment assumption into an enforced invariant.

---

## 16. Dependency & Software Supply Chain Review

### Scan results

| Tool | Scope | Result |
| ---- | ----- | ------ |
| `pip-audit` 2.10.1 | Installed venv (78 packages, incl. dev) | **1 vulnerability** — pytest 8.4.2 / PYSEC-2026-1845 |
| OSV `querybatch` API | All 77 third-party locked packages | **1 advisory** — the same one |
| `uv.lock` review | 78 packages | Fully pinned, hashes present |

### Reachability analysis (per §25 policy — not a raw CVE dump)

**PYSEC-2026-1845 / CVE-2025-71176 — pytest 8.4.2**

1. Vulnerable version in use? Yes.
2. Vulnerable functionality reachable? Only through `tmp_path`/`tmpdir` during test runs.
3. Fixed version available? Yes — 9.0.3 (a major version bump).
4. Direct or transitive? Direct, dev group only.
5. Realistic exploitability? **Very low.** Requires a hostile local user racing a predictable `/tmp`
   path on the machine running the tests. CI runners are ephemeral and single-tenant.
6. **Present in the production image? No** — `uv sync --frozen --no-dev` excludes the dev group
   entirely (`docker/Dockerfile:14, 20`).

→ Classified **Informational** (SEC-023), not Low. Reporting it as a production risk would be
inaccurate.

### Supply chain posture (OWASP SCVS / SLSA v1.2)

| Control | Status | Notes |
| ------- | ------ | ----- |
| Lockfile present and used | **PASS** | `uv.lock` committed; `--frozen` in Docker, `--locked` in CI |
| Dependencies pinned | **PASS** | Direct deps use compatible ranges; the lockfile pins exact versions with hashes |
| Transitive dependencies locked | **PASS** | All 78 resolved and pinned |
| Dev/prod separation | **PASS** | `[dependency-groups] dev` excluded from the image — exemplary |
| Abandoned / EOL packages | **PASS** | All dependencies are current, actively maintained, 2026-era releases. `types-redis` 4.6 and `types-pyopenssl` 24.1 are stale type stubs, but they are dev-only and carry no runtime risk |
| Dependency count | **PASS** | 78 total is lean for this feature set |
| Dependency confusion | **PASS** | No private package names; the project is not published to PyPI; no custom index configured |
| Typosquatting exposure | **PASS** | All direct dependencies are well-known, correctly spelled canonical packages |
| Package source configuration | **PASS** | Default PyPI; no alternate or additional index |
| Automated dependency scanning in CI | **FAIL** | None — SEC-015 |
| Secret scanning in CI | **FAIL** | None — SEC-015 |
| SAST in CI | **FAIL** | None — SEC-015 |
| Container scanning in CI | **FAIL** | None — SEC-015/SEC-016 |
| SBOM generation | **FAIL** | None produced. `uv.lock` makes generation straightforward — SCVS V1/V2 |
| Build provenance / attestation | **FAIL** | None — SLSA Build L0/L1 |
| Reproducible builds | **PARTIAL** | Python deps are reproducible via the lockfile; the base image is not pinned by digest — SEC-016 |
| Signed releases | **FAIL** | No artifact signing |
| Dependabot / Renovate | **FAIL** | Not configured |

**SLSA assessment: approximately Build L1.** The build is scripted and the dependency set is fully
locked, which is the hard part. What is missing is provenance generation, artifact signing, and
digest-pinned inputs — all incremental additions to the existing workflow, not architectural changes.

**Summary.** The dependency *state* is excellent — one dev-only advisory across 78 packages is an
unusually clean result. The dependency *process* is the gap: nothing in CI would detect the day this
changes.

---

## 17. CI/CD Security Review

### What is done well

- `permissions: contents: read` declared at the workflow level — least privilege by default, and
  notably better than the common `write-all` default.
- `pull_request`, **not** `pull_request_target` — this is the single most important GitHub Actions
  security decision, and it is correct. Fork PRs cannot access secrets.
- **No secrets are referenced anywhere** in the workflow, so there is nothing to exfiltrate or leak
  into logs.
- `concurrency` with `cancel-in-progress` — prevents runner exhaustion from rapid pushes.
- `astral-sh/setup-uv` is SHA-pinned with a version comment — the correct pattern, already understood
  by the authors and simply not applied uniformly.
- `uv sync --locked` — CI fails if the lockfile is out of date, preventing silent drift.
- Job dependencies (`needs: quality`) gate expensive jobs behind fast checks.
- No self-hosted runners; GitHub-hosted runners are ephemeral and single-tenant.

### Findings

| Check | Status | Notes |
| ----- | ------ | ----- |
| Workflow permissions | **PASS** | `contents: read` |
| Fork PR handling | **PASS** | `pull_request`, no secret access |
| Secrets exposure | **PASS** | No secrets used |
| Untrusted input in shell commands | **PASS** | The only interpolation is `${{ matrix.environment }}`, whose values are workflow-defined constants — not attacker-controllable |
| Script injection | **PASS** | No `github.event.*` interpolation into `run:` blocks |
| Mutable third-party actions | **FAIL** | 4 of 5 unpinned — SEC-015 |
| Artifact poisoning | **PASS** | No artifacts uploaded or downloaded |
| Cache poisoning | **PARTIAL** | `enable-cache: true` on setup-uv; cache is keyed on the lockfile and scoped per-branch by GitHub, so risk is low |
| Privileged / self-hosted runners | **PASS** | GitHub-hosted only |
| Environment protection rules | **N/A** | No deployment jobs exist |
| Deployment approval | **N/A** | CI does not deploy |
| Production secrets in CI | **PASS** | None present |
| Branch protection | **UNKNOWN** | Not visible in-repo — confirm with the repository owner |
| Release signing | **FAIL** | No signing — SEC-015 |
| Build provenance | **FAIL** | No attestation — SEC-015 |
| Security gates (SCA/SAST/secrets/container) | **FAIL** | None — SEC-015 |
| Coverage gate | **PARTIAL** | `--cov-fail-under=60` is low for security-relevant code |

**Can CI secrets leak into logs or artifacts?** No — there are none. This is the strongest property of
the current pipeline and should be preserved: if deployment is added later, use OIDC federation rather
than long-lived secrets, and keep deployment in a separate workflow with its own environment
protection rules.

---

## 18. Infrastructure / Container Security Review

### Docker

| Check | Status | Notes |
| ----- | ------ | ----- |
| Runs as root | **PASS** | Dedicated system user with `/usr/sbin/nologin`; `USER runtime` set |
| Ownership before user switch | **PASS** | `chown -R runtime:runtime /app` precedes `USER` — correctly ordered |
| Privileged mode | **PASS** | Not used |
| Unnecessary capabilities | N/A at image level | See SEC-011 for the runtime gap |
| Secrets baked into image | **PASS** | None; all configuration is environment-based |
| Dev dependencies in image | **PASS** | `--no-dev` — excludes the vulnerable pytest |
| Multi-stage / layer caching | **PASS** | Dependencies installed before source is copied |
| Base image | **PARTIAL** | `python:3.12-slim` is a reasonable minimal choice, but the tag is mutable — SEC-016 |
| Image size | **PASS** | `-slim` base |
| `HEALTHCHECK` | **FAIL** | Absent — SEC-016 (Kubernetes probes cover the chart path) |
| Writable filesystem | **FAIL** at runtime | SEC-011 |
| Exposed ports | **PASS** | No `EXPOSE`; the chart declares `containerPort: 8000` explicitly |
| `.dockerignore` | **MISSING** | Without one, `COPY src ./src` is narrow enough that no secrets are pulled in, but adding one is good hygiene |

### Kubernetes (Helm chart)

| Check | Status | Notes |
| ----- | ------ | ----- |
| Privileged pods | **PASS** | Not requested |
| `hostPath` / `hostNetwork` / `hostPID` | **PASS** | None used |
| `securityContext` | **FAIL** | Entirely absent — SEC-011 |
| `automountServiceAccountToken` | **FAIL** | Not disabled — SEC-011 |
| RBAC | **PASS by omission** | No Role or RoleBinding is created, so the ServiceAccount has no explicit permissions — correct, since none are needed. The mounted token is the residual risk |
| Secret handling | **PARTIAL** | The chart correctly **never creates** credential values and requires a pre-existing Secret — a genuinely good decision. But it distributes it to every workload — SEC-012 |
| ConfigMap/Secret separation | **PASS** | Non-sensitive config in a ConfigMap, credentials in a Secret |
| Network policies | **FAIL** | None — SEC-019 |
| Resource requests/limits | **PASS** | Set for all four workloads — a real availability control |
| Liveness / readiness probes | **PASS** | HTTP probes for the API; `kill -0 1` for background processes. The latter only proves PID 1 exists, not that the loop is healthy — consider a heartbeat file or a metrics-based check |
| Service type | **PASS** | `ClusterIP` — not exposed externally by default |
| Migration ordering | **PASS** | `helm.sh/hook: pre-install,pre-upgrade` with `hook-weight: -5` — migrations provably complete before workloads start |
| Job TTL / backoff | **PASS** | `ttlSecondsAfterFinished: 300`, `backoffLimit: 2` |
| Image tag | **PARTIAL** | Mutable tag with `IfNotPresent` — SEC-016 |
| `PodDisruptionBudget` | **FAIL** | Absent — availability rather than security |
| HPA | Present for workers | Correctly optional, disabled by default |
| Auth mode default | **PASS** | `authMode: api_key` — the production path is secure by default |

### Terraform

| Check | Status | Notes |
| ----- | ------ | ----- |
| Wildcard IAM permissions | N/A | No IAM resources created |
| Excessive privileges | N/A | Namespace and quota only |
| Publicly accessible resources | **PASS** | None created |
| Exposed storage buckets | N/A | None created |
| State backend security | **PASS** | HTTP backend with credentials held outside Git; `backend.hcl.example` carries placeholders and clear instructions |
| Secrets in state | **PASS** | Explicitly avoided — Terraform receives only the Secret's *name*, and the output is documented as "Secret name only" |
| Provider version pinning | **PASS** | `~> 2.35` plus a committed `.terraform.lock.hcl` |
| Terraform version constraint | **PASS** | `>= 1.9.0, < 2.0.0` |
| Input validation | **PASS** | The `namespace` variable validates DNS compatibility and length; `environment` is pinned per root module |
| Namespace security posture | **FAIL** | No PSA labels, NetworkPolicy, or LimitRange — SEC-019 |
| IaC scanning in CI | **FAIL** | `terraform validate` only; no `checkov`/`tfsec` — SEC-015 |
| Checked-in provider binaries | **Hygiene issue** | ~100 MB of `.terraform/providers/**` in the tree despite the gitignore rule |

---

## 19. Logging & Monitoring Review

### Security events

| Event | Logged | Where | Notes |
| ----- | ------ | ----- | ----- |
| Authentication success | **Yes** | `security_audit_events` | Only in `api_key` mode |
| Authentication failure | **Yes** | `security_audit_events` | Both `AUTHENTICATION_REQUIRED` and `INVALID_CREDENTIAL` |
| Rate limit exceeded | **Yes** | `security_audit_events` | With the client ID |
| Rate limiter unavailable | **Yes** | `security_audit_events` | Fails closed with `503` — a good decision |
| Oversized request | **Yes** | `security_audit_events` | `REQUEST_TOO_LARGE` |
| **Audit write failure** | **Partially** | stdout only | Swallowed; no metric, no identifying data — SEC-007 |
| Run lifecycle transitions | **Yes** | `run_events` (append-only) | Comprehensive: queued, claimed, succeeded, failed, retry scheduled, dead-lettered, replayed |
| Outbox publish success/failure | **Yes** | `run_events` | Including the error *type* (not the message) |
| Permission changes | N/A | — | No permission model |
| Admin actions | N/A | — | No admin tier |
| Token revocation | N/A | — | No revocation mechanism |
| Sensitive record access (reads) | **No** | — | `GET /v1/runs/{id}` is not audited. Given SEC-001, read auditing is exactly what would detect cross-tenant access |
| Configuration changes | **No** | — | Out of process scope |

### Sensitive data in logs

| Data | Present? | Mechanism |
| ---- | -------- | --------- |
| Passwords | No | None exist |
| Raw API keys | **No** | Hashed immediately; never passed to a logger. Asserted by `test_..._does_not_leak_raw_key` |
| Refresh tokens | No | None exist |
| Prompts / `input_payload` | **No** | Formatter allowlist: `event`, `run_id`, `attempt_id`, `provider`, `error_code` only |
| Provider responses | **No** | Never logged; error bodies never even retained |
| PII | **No** | Cannot reach logs through the allowlist |
| Connection strings | **No** | `SecretStr` prevents accidental repr |

The `JsonFormatter` allowlist is a strong, well-tested control and is the reason this table reads as
it does. It also causes SEC-013 (tracebacks discarded) — a trade-off worth rebalancing, not reversing.

### Log injection and tampering

| Risk | Status |
| ---- | ------ |
| Log injection via user input | **PASS** | Only allowlisted server-controlled fields are emitted; output is `json.dumps`-encoded, so newline/control-character injection is impossible |
| Log injection via `tracestate` | **PARTIAL** | Caller-controlled `tracestate` is persisted and forwarded to the collector, which the Compose config runs at `verbosity: detailed` — SEC-014 |
| Audit tampering (application) | **PASS** | Database triggers reject `UPDATE`/`DELETE` on `run_events`, `run_attempts` and `security_audit_events` |
| Audit tampering (compromised app role) | **FAIL** | The app role owns the triggers and can disable them — SEC-018 |
| Audit suppression | **FAIL** | SEC-007 |

### Monitoring and alerting

Metrics are well designed: `RuntimeMetrics` restricts every label to a known-value set
(`safe_provider`, `safe_error_code` both fall back to `"other"`), and run/attempt IDs are explicitly
excluded — a deliberate, tested defence against metric-cardinality exhaustion.

Missing, in order of value:
1. **Security metrics.** No counters for authentication failures, rate-limit denials, or audit-write
   failures — none of the `security_audit_events` outcomes are exported to Prometheus.
2. **Alerting.** The Grafana dashboard is provisioned read-only with no alert rules.
3. **Detection signals** that would have caught the findings above: a spike in
   `arr.retries.scheduled` (SEC-004), a spike in `arr.attempts.started` (SEC-003), sustained API
   p99 latency with a flat request rate (SEC-002), or many distinct rate-limit buckets under one
   credential (SEC-006).

**Recommendation:** export the audit outcomes as a labelled counter
(`arr.security.events{event_type, outcome}`) and add alert rules for authentication-failure rate,
audit-write failures (any non-zero value), and retry-scheduling rate. All three are small additions to
existing, well-built machinery.

---

## 20. Data Protection & Privacy Review

| Aspect | Status | Notes |
| ------ | ------ | ----- |
| Data classification | **Missing** | No documented classification for prompts or results |
| Encryption in transit (client → API) | **Not enforced by the app** | No TLS termination in-process; assumes an ingress |
| Encryption in transit (internal) | **Not enforced** | `redis://` and `amqp://` permitted; OTel defaults to `http://` |
| Encryption at rest | **FAIL** | Plaintext JSONB — SEC-022 |
| Data minimization (logs) | **PASS** | Allowlist |
| Data minimization (metrics) | **PASS** | Cardinality control |
| Data minimization (traces) | **PASS** | No payload capture |
| Data minimization (queue) | **PASS** | Messages carry only `event_id`, `run_id`, `trace_context` |
| Data minimization (provider) | **PASS** | `store: false`; only id/model/text/tokens retained |
| Data minimization (storage) | **FAIL** | Input duplicated into `result_payload` by the default provider — SEC-022 |
| Retention policy | **FAIL** | None; nothing expires |
| Right to erasure | **FAIL** | No deletion path; `ON DELETE RESTRICT` + append-only triggers make it architecturally hard |
| Data subject access | **PARTIAL** | A tenant can read their own runs — but so can any other tenant (SEC-001) |
| Cross-tenant leakage | **FAIL** | SEC-001 |
| PII in audit records | **PASS** | Only `client_id` and a fingerprint |
| Third-party data sharing | **PASS and documented** | Prompts go to OpenAI only when explicitly routed there; `store: false` limits provider-side retention; the default provider makes no network call at all |
| Backup protection | **Out of scope** | Not managed by this repository |

**The credentials-free default deserves credit.** Because `deterministic` is the default provider, a
deployment that is not explicitly configured for OpenAI sends **no** data to any third party. That is
the right default for a system handling potentially sensitive prompts.

**The main privacy gap is lifecycle.** The system is designed for durability — append-only events,
`RESTRICT` foreign keys, immutable snapshots — and those properties are in direct tension with
erasure obligations. This should be resolved deliberately (crypto-shredding is the standard
reconciliation) rather than discovered during a data-subject request.

---

## 21. Security Controls That Are Already Good

Listing only problems would misrepresent this repository. The following controls are implemented
correctly and should be preserved through any remediation.

1. **Constant-time credential comparison.** `hmac.compare_digest` in `authenticate_api_key` —
   the correct primitive, verified constant-time in practice.
2. **The raw API key is never retained.** Hashed on arrival, never stored, logged, or traced, with a
   regression test asserting it.
3. **Log field allowlisting.** `JsonFormatter` emits only five domain fields. Prompts, payloads and
   secrets provably cannot reach stdout — enforced for every library in the process.
4. **Metric cardinality control.** `safe_provider` and `safe_error_code` collapse unknown values to
   `"other"`; run and attempt IDs are explicitly forbidden as labels, with a test enforcing it.
5. **Provider error sanitization.** `ProviderHttpError` carries only a status code. Provider response
   bodies are never persisted, logged, or traced — closing a very common leak path.
6. **Defensive provider-response parsing.** Every field is individually type-guarded; a malicious
   upstream response cannot inject unexpected types into persisted state.
7. **`store: false` on OpenAI requests.** Limits provider-side prompt retention by default.
8. **Append-only database triggers.** `run_events`, `run_attempts` and `security_audit_events` reject
   `UPDATE` and `DELETE` at the database level, not in application code.
9. **Transactional outbox.** Run creation and delivery intent commit atomically — no lost work, and no
   dual-write inconsistency.
10. **Worker lease ownership verification.** `_owns_active_lease` requires a matching `worker_id` and
    an unexpired lease before any terminal write.
11. **Row-level locking discipline.** `FOR UPDATE` for claims, `FOR UPDATE SKIP LOCKED` for batch
    scanners — correct in every one of the four places it appears.
12. **Optimistic concurrency.** `version_id_col` on `Run`.
13. **Idempotency enforced by a database constraint**, with the `IntegrityError` race path handled
    explicitly and tested concurrently.
14. **Order-independent request hashing.** `canonical_request_hash` sorts keys, so idempotency is
    semantic rather than byte-literal.
15. **Strict `Idempotency-Key` validation.** A precise regex with clear bounds — the model the other
    headers should follow.
16. **`extra="forbid"` on every top-level request model.** Blocks mass assignment at the outer layer.
17. **Conservative retry classification.** Auth failures and `4xx` never retry; only timeouts, `429`
    and `5xx` do. Prevents retry storms against a failing provider.
18. **Explicit state machine.** `ALLOWED_EXECUTION_TRANSITIONS` with exhaustive tests for both allowed
    and forbidden transitions.
19. **Server-owned routing metrics.** Callers choose among candidates but cannot supply the
    latency/cost/quality figures that drive selection.
20. **Rate limiter fails closed.** Redis unavailability returns `503`, never silently allows.
21. **Dead-letter and poison-message topology.** Malformed messages are rejected without requeue and
    routed to a durable dead-letter queue — no infinite redelivery loop.
22. **Non-root container user** with `/usr/sbin/nologin`, and correct `chown` ordering.
23. **Dev dependencies excluded from the production image** via `--no-dev` — which is precisely why
    the one dependency advisory is not a production risk.
24. **Locked, hash-pinned dependencies** with `--frozen`/`--locked` enforcement in both Docker and CI.
25. **Helm never creates secret values.** Requires a pre-provisioned Secret, keeping credentials out of
    chart values and Git.
26. **Terraform never reads secret values.** Only names flow through state; documented explicitly.
27. **Migrations run as a pre-install/pre-upgrade hook**, so application processes can never start
    against an unmigrated schema.
28. **CI least privilege.** `contents: read`, `pull_request` not `pull_request_target`, and no secrets
    referenced anywhere.
29. **Clean Git history.** No credential was ever committed across all 21 commits.
30. **Architecture enforced by test.** `test_domain_does_not_depend_on_delivery_frameworks` keeps the
    domain layer free of framework coupling — a maintainability control with real security value.
31. **Credentials-free default provider.** An unconfigured deployment sends no data to any third party.
32. **Documented security posture.** `SECURITY.md`, ADR-0004, and the README state the intended
    boundary honestly, including its limitations. The ADR's own framing of `X-Client-Id` is what made
    SEC-001 straightforward to characterize precisely.

---
## 22. OWASP Top 10:2025 Coverage Matrix

| Category | Status | Findings | Notes |
| -------- | ------ | -------- | ----- |
| **A01 — Broken Access Control** | **FAIL** | SEC-001, SEC-003 | Tenant isolation rests entirely on a caller-supplied header; the costliest endpoint has no access control at all. The most serious category for this system |
| **A02 — Cryptographic Failures** | **PARTIAL** | SEC-010, SEC-022 | No custom crypto, no deprecated algorithms, correct constant-time comparison. Weaknesses: unsalted single-round SHA-256 verifier, no encryption at rest, TLS not enforced for internal links |
| **A03 — Injection** | **PARTIAL** | SEC-002 | Classic injection families are all clean (no raw SQL, no command execution, no deserialization, no templating). Caller-supplied regex and JSON Schema are executed unbounded — injection of *expressions* rather than of data |
| **A04 — Insecure Design** | **FAIL** | SEC-002, SEC-003, SEC-004, SEC-005, SEC-006, SEC-020 | The dominant theme. The system is designed against failure, not against a hostile caller: no quotas, no budgets, no caps, no deadlines, and caller-authored execution policy |
| **A05 — Security Misconfiguration** | **PARTIAL** | SEC-008, SEC-009, SEC-011, SEC-012, SEC-019, SEC-021 | Fail-open auth default, anonymous OpenAPI docs, no `securityContext`, over-distributed Secret, no PSA/NetworkPolicy. Offset by a correct `authMode: api_key` chart default and ClusterIP-only exposure |
| **A06 — Vulnerable and Outdated Components** | **PASS** | SEC-023 (Info) | One advisory across 77 third-party packages, dev-only and excluded from the production image. An unusually clean dependency tree |
| **A07 — Identification and Authentication Failures** | **PARTIAL** | SEC-001, SEC-008, SEC-010 | The credential check is correct in isolation but produces no principal; one shared key; no rotation or revocation; fail-open default |
| **A08 — Software and Data Integrity Failures** | **PARTIAL** | SEC-015, SEC-016 | Lockfile discipline is strong and dev/prod separation is exemplary. Missing: SHA-pinned actions, digest-pinned base image, SBOM, signing, provenance |
| **A09 — Security Logging and Monitoring Failures** | **PARTIAL** | SEC-007, SEC-013, SEC-014 | Append-only audit tables and a strict log allowlist are real strengths. Undermined by silently swallowed audit writes, discarded tracebacks, no read auditing, no security metrics, and no alerting |
| **A10 — Mishandling of Exceptional Conditions** | **PARTIAL** | SEC-005, SEC-007, SEC-017 | Mostly good: the rate limiter fails closed, retries are conservatively classified, transactions are correct, the state machine is explicit, and internal errors return a generic `500`. Gaps: `Content-Length` parse failure fails *open*, audit failures are swallowed, and an over-length header produces a `500` instead of a `422` |

**Score: 1 PASS, 8 PARTIAL, 2 FAIL** *(A01 and A04 are the two FAILs; A06 is the single PASS.)*

---

## 23. ASVS 5.0.0 Coverage

Assessed at **Level 1** with selected Level 2 controls, limited to what static and in-process dynamic
review can evidence. Chapters with no evidence are marked `NOT TESTED` — this is not an
item-by-item certification.

| ASVS area | Status | Evidence / gap |
| --------- | ------ | -------------- |
| V1 — Encoding and Sanitization | **PARTIAL** | No injection sinks except the regex/schema evaluator (SEC-002); JSON output encoding is correct |
| V2 — Validation and Business Logic | **PARTIAL** | Strong state machine, idempotency and transactional integrity; missing quotas, magnitude bounds and anti-automation (SEC-004, SEC-006) |
| V3 — Web Frontend Security | **NOT APPLICABLE** | JSON-only API; no HTML, cookies, CSRF surface, CSP, or framing concerns. `/docs` is the only HTML served (SEC-009) |
| V4 — API and Web Service | **PARTIAL** | Consistent REST semantics, strict content types, correct status codes; no pagination; anonymous schema exposure |
| V5 — File Handling | **NOT APPLICABLE** | **No file upload or download anywhere in the application.** The only filesystem access is the operator CLI writing a report |
| V6 — Authentication | **PARTIAL** | Constant-time comparison and no raw-key retention (PASS); weak KDF, no rotation, no entropy requirement, fail-open default (FAIL) — SEC-008, SEC-010 |
| V7 — Session Management | **NOT APPLICABLE** | Fully stateless; no sessions, tokens, or cookies |
| V8 — Authorization | **FAIL** | V8.1 (design), V8.2 (operation-level), V8.3 (object-level) all fail — SEC-001, SEC-003 |
| V9 — Self-contained Tokens | **NOT APPLICABLE** | No JWT or other self-contained tokens |
| V10 — OAuth and OIDC | **NOT APPLICABLE** | Not implemented |
| V11 — Cryptography | **PARTIAL** | No custom crypto, no weak algorithms, CSPRNG identifiers (PASS); weak credential KDF, no encryption at rest (FAIL) |
| V12 — Secure Communication | **PARTIAL** | TLS not enforced by the application for client, Redis, AMQP or OTLP links; assumed from the deployment environment |
| V13 — Configuration | **PARTIAL** | Secrets kept out of Git, `SecretStr` used, ConfigMap/Secret separated (PASS); insecure default, over-distributed Secret, a setting that does nothing (FAIL) — SEC-008, SEC-012, SEC-020 |
| V14 — Data Protection | **PARTIAL** | Excellent minimization in logs, metrics, traces and queue messages; no encryption at rest, no retention policy, no erasure path (SEC-022) |
| V15 — Secure Coding and Architecture | **PARTIAL** | Strict mypy, Ruff, clean layering with an architecture test, no dangerous constructs (PASS); caller-supplied expressions executed unbounded (FAIL) |
| V16 — Security Logging and Error Handling | **PARTIAL** | Append-only audit and strict allowlisting (PASS); swallowed audit failures, discarded tracebacks, no read auditing (FAIL) — SEC-007, SEC-013 |
| V17 — WebRTC | **NOT APPLICABLE** | Not used |
| Runtime/production configuration | **NOT TESTED** | No access to a deployed environment |
| Network and firewall controls | **NOT TESTED** | Outside the repository |
| Backup and recovery controls | **NOT TESTED** | Not defined in the repository |
| Cloud IAM policies | **NOT TESTED** | Terraform creates none |
| Physical and personnel controls | **NOT TESTED** | Out of scope |

---

## 24. Tool Results

| Tool | Version | Scope | Result |
| ---- | ------- | ----- | ------ |
| **Semgrep** | 1.177.0 | `src tests docker charts infra scripts migrations examples .github` — rulesets `p/python`, `p/security-audit`, `p/secrets`, `p/dockerfile`, `p/terraform` | **0 findings.** 310 rules, 90 targets, ~100% parse rate |
| **pip-audit** | 2.10.1 | Installed virtualenv (78 packages including dev) | **1 vulnerability:** pytest 8.4.2 → PYSEC-2026-1845 (fix 9.0.3) |
| **OSV `querybatch`** | API, 2026-09-12 | All 77 third-party locked packages | **1 advisory** — the same pytest issue. 76 packages clean |
| **Git history scan** | `git log -p --all` + credential regexes | All 21 commits, all branches | **0 live credentials.** Only test fixtures and documentation text |
| **pytest** | 8.4.2 | Full suite | **128 passed** — healthy baseline confirmed before assessment |
| **Manual code review** | — | 100% of `src/` (26 modules), 6 migrations, all IaC, all CI, all Helm templates | 23 findings |
| **In-process verification** | Custom harness, 17 experiments | API middleware, evaluation engine, policy builder, auth, rate limiter, audit sink | **15 confirmed, 2 refuted** |
| **DAST / OWASP ZAP** | — | — | **NOT TESTED.** Running the Compose stack and scanning it would have meant starting live PostgreSQL, Redis and RabbitMQ containers and issuing active probes. In-process verification against `create_app()` provided equivalent or better evidence for every candidate finding without that exposure |
| **Container image scan (Trivy)** | — | — | **NOT TESTED.** Would require building the image; the base image is a mutable tag, so results would not be reproducible against this revision. Recommended for CI — SEC-015 |
| **IaC scan (checkov/tfsec)** | — | — | **NOT TESTED.** Terraform was reviewed manually; the module is 32 lines. Recommended for CI |
| **gitleaks / TruffleHog** | — | — | **NOT INSTALLED.** Substituted with Semgrep's `p/secrets` ruleset plus targeted regex scanning of the full Git history. Both returned clean |

### Interpreting the Semgrep result

Zero findings across 310 rules is the most informative automated result in this audit. It is not
evidence of security; it is evidence about *where* the security risk lives. Pattern-based SAST detects
dangerous constructs — `eval`, string-built SQL, `shell=True`, `pickle.loads`, hardcoded secrets.
This codebase contains none of them.

Every High finding in this report is a property of the system's **design**: which identity is trusted,
which limits are enforced, and where expensive work runs. No pattern matcher detects "this string
comes from a header and is used as an authorization key," or "this configuration setting is never
read." That is why §22 of the source-review methodology mandates manual review independent of scanner
output, and why the four High findings came from reading code rather than from running tools.

### CVSS v4.0 scoring note

Every finding carries a CVSS v4.0 **vector**. Numeric base scores are deliberately omitted: CVSS v4.0
computes scores through an official macrovector lookup table of 270 entries, and reproducing those
values from memory risks publishing numbers that disagree with the official calculator. The vectors
are reproducible statements of the assessment; paste any of them into the FIRST CVSS v4.0 calculator
for an authoritative score. Severity ratings follow §26 and consider business context that CVSS does
not capture.

---

## 25. False Positives and Items Requiring Manual Validation

Per the false-positive policy, candidates were reproduced before being reported. The following were
investigated and **rejected** — they are documented here so they are not re-raised in a future audit.

### Rejected: SSRF via remote `$ref` in caller-supplied JSON Schema

**Hypothesis.** `evaluate_rules` passes a caller-controlled schema to `Draft202012Validator`. Older
`jsonschema` releases resolved remote `$ref` URIs over the network via `urlopen`, which would have made
`{"$ref": "http://169.254.169.254/latest/meta-data/"}` a clean SSRF into the cloud metadata service —
a High-severity finding.

**Test (experiment V3).** A schema containing exactly that `$ref` was evaluated in-process:

```
raised _WrappedReferencingError: Unresolvable: http://169.254.169.254/latest/meta-data/
```

**Conclusion: FALSE POSITIVE.** `jsonschema` 4.26.0 uses the `referencing` library, which resolves
only against an explicitly supplied registry and raises `Unresolvable` for remote URIs unless a
retriever is configured. The code supplies none. **No SSRF exists.**

*Caveat for future audits:* this safety is a property of the pinned library version, not of the
application code. If `jsonschema` is ever replaced or configured with a retriever, this becomes
exploitable. Consider adding a test that asserts remote `$ref` resolution stays disabled.

### Rejected: Timing attack on API key comparison

**Hypothesis.** A byte-wise credential comparison would leak the key through response timing.

**Test (experiment V17).** 2,000 failed authentications completed in 0.001 s with no measurable
variance by prefix length. `hmac.compare_digest` is used.

**Conclusion: FALSE POSITIVE.** Correctly implemented.

### Rejected: SQL injection

**Hypothesis.** Dynamic query or filter construction somewhere in the persistence layer.

**Test.** Exhaustive search: `sa.text` is never imported in `src/`; no f-string, `%`-format, or
`.format()` produces SQL; every query uses SQLAlchemy expression constructs with bound parameters.

**Conclusion: FALSE POSITIVE. No SQL injection exists.**

### Rejected: Command injection and unsafe deserialization

**Test.** No `subprocess`, `os.system`, `os.popen`, `shell=True`, `eval`, `exec`, `compile`,
`__import__`, `pickle`, `marshal`, or `yaml.load` anywhere in `src/`. Only `json.loads`.

**Conclusion: FALSE POSITIVE for both.**

### Rejected: Caller-controlled provider base URL (SSRF)

**Hypothesis.** `policy.provider_order` or another policy key could redirect provider traffic.

**Test.** `OpenAIResponsesProvider._base_url` derives solely from `settings.openai_base_url`.
`ProviderRegistry` resolves adapters by *name* from a fixed dictionary and raises on unknown names.
`resolve_routing_decision` rejects any provider absent from the server-owned catalog.

**Conclusion: FALSE POSITIVE.** A caller can select among configured providers but cannot introduce a
destination.

### Items requiring manual validation

| Item | Why it could not be fully confirmed | Suggested validation |
| ---- | ----------------------------------- | -------------------- |
| **SEC-017** (`runs.client_id` over-length → `500`) | Confirmed in the audit-sink path and by column definition, but the `runs` insert path was not exercised against a live PostgreSQL instance — the in-process harness used a stub service | Submit a 129-character `X-Client-Id` against the Compose stack and observe the status code |
| Redis `INCR`/`EXPIRE` non-atomicity (SEC-006) | Reasoned from code; not reproduced against live Redis | Kill the API between the two commands and check `TTL` on the key |
| `readOnlyRootFilesystem` compatibility (SEC-011) | Not tested; the OTel SDK or `uv` runtime may require writable paths beyond `/tmp` | Apply the `securityContext` in a staging cluster and confirm all four processes start |
| Actual OpenAI spend rate (SEC-003) | Deliberately not tested — would incur real cost and call a third-party system | Model the cost from the operator's pricing tier and the 200-call figure |
| Branch protection rules | Not visible in the repository | Confirm with the repository owner |
| Production TLS, ingress and network policy | Outside the repository | Review the deployment environment |
| Database backup encryption and access | Outside the repository | Review the managed-database configuration |
| Real-world entropy of the deployed API key (SEC-010) | The digest reveals nothing about the key's strength | Confirm with the operator how the key was generated |

---

## 26. Severity Model

Severity was assigned by weighing exploitability, required privileges, network exposure, data
sensitivity, business impact, blast radius, tenant impact and production reachability together — not
by vulnerability class alone.

Concretely, this is why certain findings landed where they did:

- **SEC-001 is High, not Critical.** It requires the shared credential, and run IDs are UUIDv4 rather
  than enumerable. Under the default `auth_mode=disabled` it would be Critical, but the *supported
  production path* (the Helm chart) enables authentication, and severity is rated against the
  supported path.
- **SEC-002 is High, not Medium.** Availability-only findings are often Medium, but this one blocks
  the event loop — so one tenant's request degrades every tenant — is reachable by any authenticated
  caller with no special conditions, and can drive a pod crash loop through failed liveness probes.
- **SEC-003 is High** because the impact is direct financial loss with no cap, not merely resource
  consumption, and because the endpoint has *no* access control rather than weak access control.
- **SEC-004 is High, not Critical**, because the effect is delayed and highly visible in existing
  dashboards (`arr.retries.scheduled` would spike immediately), meaningfully raising the chance of
  detection before serious damage.
- **SEC-011 is Medium, not High**, because no code-execution vector currently exists in this codebase.
  It is an escalation multiplier, and its CVSS vector reflects subsequent-system impact rather than
  vulnerable-system impact.
- **SEC-023 is Informational, not Low**, because reachability analysis showed the vulnerable package is
  excluded from the production image entirely. Rating it higher would misdirect remediation effort.
- **SEC-021 is Informational**, because it is an explicit, documented, and defensible development-only
  choice — not a defect. Reporting a documented dev-stack decision as a vulnerability would dilute the
  findings that matter.
- **No finding is Critical.** Nothing found permits unauthenticated remote code execution, full
  database compromise, or unauthenticated total data exfiltration in the supported configuration.

---

## 27. Remediation Roadmap

### NOW — Immediate (before any production exposure)

| Order | Finding | Action | Effort |
| ----- | ------- | ------ | ------ |
| 1 | **SEC-001** | Bind identity to the credential; remove `X-Client-Id` as an authorization input | M |
| 2 | **SEC-002** | Replace the backtracking regex engine (RE2) or allowlist safe patterns; bound schema complexity | S |
| 3 | **SEC-003** | Require an authenticated identity on `/v1/evaluation-regressions`; make the rate-limit guard unconditional; lower the case cap | S |
| 4 | **SEC-004** | Replace the free-form `policy` dict with a bounded Pydantic model; clamp caller values to server limits | S |
| 5 | **SEC-008** | Invert the `auth_mode` default to `api_key` | XS |
| 6 | **SEC-012** | Split the Secret; remove the provider key from the API, dispatcher and scheduler | S |
| 7 | **SEC-005** | Count bytes actually read; fail closed on a malformed `Content-Length` | S |
| 8 | **SEC-006** | Key the rate limiter on the credential, not the header | XS *(falls out of SEC-001)* |

Items 1, 3 and 6 together remove the financial-loss path. Items 2, 4, 5 and 7 remove the denial-of-service paths.

### NEXT — Important security debt (next iteration)

| Order | Finding | Action | Effort |
| ----- | ------- | ------ | ------ |
| 9 | **SEC-007** | Validate `X-Client-Id`; truncate in the sink; define and document the audit failure policy; add a metric | S |
| 10 | **SEC-011** | Add `securityContext` and `automountServiceAccountToken: false` to all pod specs | S |
| 11 | **SEC-010** | Move to a KDF with a salt/pepper; stop storing the verifier in audit rows; require key entropy | M |
| 12 | **SEC-020** | Wire `provider_timeout_seconds` through to the HTTP client; add a config-coverage test | XS |
| 13 | **SEC-009** | Disable the docs routes in `api_key` mode; convert the middleware prefix check to an allowlist | XS |
| 14 | **SEC-015** | SHA-pin all actions; add SCA, secret-scanning, SAST and container-scanning jobs | S |
| 15 | **SEC-017** | Add a strict `CLIENT_ID_PATTERN`; handle `DataError` as `422` | XS |
| 16 | — | Add pagination to `/attempts`, `/events`, `/evaluations` | S |
| 17 | — | Add per-client concurrent-run and provider-call quotas | M |
| 18 | — | Export security audit outcomes as metrics; add alert rules | S |

### LATER — Hardening and defence in depth

| Order | Finding | Action | Effort |
| ----- | ------- | ------ | ------ |
| 19 | **SEC-019** | Add PSA labels, default-deny NetworkPolicy and a LimitRange *(after SEC-011)* | S |
| 20 | **SEC-018** | Separate `arr_migrator` and `arr_runtime` database roles | M |
| 21 | **SEC-016** | Digest-pin the base image; deploy by digest; add a `HEALTHCHECK`; generate an SBOM | S |
| 22 | **SEC-013** | Add sanitized exception context to the log formatter; record exceptions on worker spans | XS |
| 23 | **SEC-014** | Validate `traceparent`; cap `tracestate`; decide the inbound trace-trust policy | XS |
| 24 | **SEC-022** | Make the deterministic echo opt-in; add encryption at rest, a retention policy and a crypto-shredding erasure path | L |
| 25 | **SEC-021** | Bind Compose host ports to `127.0.0.1`; set a Grafana admin password | XS |
| 26 | **SEC-023** | Upgrade pytest to ≥ 9.0.3 | XS |
| 27 | — | Add audit logging for sensitive reads | S |
| 28 | — | Enforce `rediss://`/`amqps://` when `auth_mode == "api_key"` | XS |
| 29 | — | Add SLSA provenance attestation and artifact signing | M |
| 30 | — | Add a `PodDisruptionBudget`; improve background-process probes beyond `kill -0 1` | XS |
| 31 | — | Remove checked-in Terraform provider binaries from the working tree | XS |

---

## 28. Quick Wins

Each of these is under an hour and materially improves posture.

1. **Invert the auth default** (SEC-008) — one line in `settings.py`. `model_post_init` already fails
   fast when the hash is missing, so a misconfigured deployment refuses to start instead of running
   open.
2. **Disable the docs routes in `api_key` mode** (SEC-009) — three keyword arguments to `FastAPI()`.
3. **Convert the middleware prefix check to an allowlist** (SEC-009) — `if request.url.path in
   PUBLIC_PATHS` instead of `startswith("/v1/")`. Makes every future route protected by default.
4. **Wire the provider timeout** (SEC-020) — pass `timeout=` to `httpx.AsyncClient`. Turns a
   configuration knob that does nothing into a real control.
5. **Fail closed on a malformed `Content-Length`** (SEC-005 partial) — change `is_oversized = False`
   to a rejection. One line.
6. **Add `CLIENT_ID_PATTERN`** (SEC-017, and the attacker-controlled half of SEC-007) — mirror the
   existing `IDEMPOTENCY_KEY_PATTERN` four lines above it.
7. **Truncate `client_id` and `reason` in the audit sink** (SEC-007) — two `[:128]` slices remove the
   audit-suppression primitive.
8. **Add `automountServiceAccountToken: false`** (SEC-011 partial) — one line in two templates,
   removing a cluster-API token from four workloads that never use it.
9. **Lower the regression case cap** (SEC-003 partial) — change `max_length=100` to `max_length=10`.
   Reduces the worst case from 200 provider calls to 20 while the full fix is developed.
10. **Bind Compose ports to loopback** (SEC-021) — prefix seven port mappings with `127.0.0.1:`.
11. **SHA-pin the four unpinned GitHub Actions** (SEC-015 partial) — copy the pattern already used for
    `setup-uv`.
12. **Add a backoff floor** (SEC-004 partial) — `max(delay, 0.5)` in `delay_after_attempt` makes the
    hot-loop retry storm impossible even before the policy model lands.
13. **Set a Grafana admin password** (SEC-021) — two environment variables in `docker-compose.yml`.
14. **Add `pip-audit` to CI** (SEC-015 partial) — one step; the lockfile is already there.
15. **Remove checked-in Terraform provider binaries** — `git rm -r --cached infra/terraform/**/.terraform`.

Items 1–7 and 12 alone would move SEC-005, SEC-008, SEC-009, SEC-017 and SEC-020 to resolved and
materially reduce SEC-003, SEC-004 and SEC-007 — in well under a day.

---

## 29. Recommended Security Tests

The existing suite is strong on reliability and has **no authorization tests at all**. Of 128 tests,
three are security-focused (`test_api_security_boundary.py`), and all three test the authentication
mechanism rather than the authorization model. That distribution mirrors the findings exactly.

### Authorization tests (highest value — currently zero exist)

```python
# tests/integration/test_tenant_isolation.py
@pytest.mark.parametrize(
    "method,suffix",
    [
        ("GET", ""),
        ("GET", "/attempts"),
        ("GET", "/events"),
        ("GET", "/evaluations"),
        ("POST", "/evaluations"),
        ("POST", "/replay"),
    ],
)
def test_credential_scope_cannot_be_overridden_by_header(method, suffix): ...
def test_run_created_by_one_tenant_is_invisible_to_another(): ...
def test_replay_cannot_target_another_tenants_run(): ...
def test_evaluation_cannot_mutate_another_tenants_run(): ...
def test_idempotency_keys_are_isolated_per_credential(): ...
def test_every_v1_route_requires_an_authenticated_identity(): ...  # route-table driven
```

The last one is worth special attention: enumerate `app.routes` and assert that every `/v1/` path
rejects an anonymous request. That test would have caught SEC-003 the day the endpoint was added.

### Negative and abuse tests

```python
def test_caller_policy_cannot_exceed_server_bounds(): ...  # SEC-004
def test_catastrophic_regex_is_rejected_or_bounded(): ...  # SEC-002
def test_streamed_body_without_content_length_is_rejected(): ...  # SEC-005
def test_rotating_client_id_does_not_reset_the_rate_limit(): ...  # SEC-006
def test_oversized_client_id_cannot_suppress_the_audit_trail(): ...  # SEC-007
def test_api_documentation_is_not_anonymous(): ...  # SEC-009
def test_malformed_trace_context_is_not_trusted(): ...  # SEC-014
```

### Invariant tests

```python
def test_every_declared_setting_has_a_consumer(): ...  # would have caught SEC-020
def test_no_route_outside_the_allowlist_is_anonymous(): ...  # would have caught SEC-003 and SEC-009
def test_audit_fingerprint_is_not_the_stored_verifier(): ...  # SEC-010
```

These are the highest-leverage additions: each encodes a *property* of the system rather than a
behaviour, so it catches an entire class of future regression rather than one instance.

### Unit tests

- `evaluate_rules` resource bounds for both regex and JSON Schema paths.
- `build_policy_snapshot` clamping for every bounded field.
- `RetryPolicy.delay_after_attempt` floor enforcement.
- `JsonFormatter` exception-context handling without payload leakage.

### CI security gates

Add a `security` job (see SEC-015) running `pip-audit`/`osv-scanner`, `gitleaks`, `semgrep`, and
`trivy` on the built image, plus `checkov` on Terraform and a `conftest` policy check on the rendered
Helm chart. Raise `--cov-fail-under` above 60, ideally with a stricter floor for
`src/agent_runtime/security` and `src/agent_runtime/domain`.

### Load and abuse testing

Beyond unit coverage: run a soak test issuing evaluations with pathological rules, and one submitting
runs with adversarial policies, asserting that p99 latency on unrelated endpoints stays within budget.
This is the class of testing that would surface SEC-002 and SEC-004 in staging rather than production.

---

## 30. Manual Verification Required

See the table in §25 for items that could not be fully confirmed by static and in-process review.
In summary, the following require a human with environment access:

1. **SEC-017** against a live PostgreSQL instance (the `runs` insert path).
2. **Redis atomicity** (SEC-006) under process failure.
3. **`readOnlyRootFilesystem` compatibility** (SEC-011) in a staging cluster.
4. **Actual provider spend rate** (SEC-003) — deliberately not tested to avoid third-party cost.
5. **Branch protection rules** — not visible in the repository.
6. **Production TLS, ingress, WAF and network policy** — outside the repository.
7. **Database backup encryption, retention and access control** — outside the repository.
8. **The deployed API key's real entropy** (SEC-010) — known only to the operator.
9. **Cluster RBAC** granted to the ServiceAccount beyond the chart's definition.
10. **Whether `APP_OPENAI_API_KEY` is actually configured** in any live deployment — this determines
    whether SEC-003 is a financial or an availability finding in practice.

---

## 31. Limitations

This audit is honest about what it could not see.

**Not observable from the repository:**

- Production and staging runtime configuration — the actual `APP_*` environment values in use.
- The contents of the pre-provisioned Kubernetes Secret.
- Cloud IAM policies, cluster RBAC bindings, and admission controllers.
- Network firewalls, security groups, service mesh policy, ingress and WAF configuration.
- TLS termination, certificate management, and cipher configuration.
- Database backup, retention, and encryption settings.
- Monitoring, alerting, and on-call response in practice.
- Branch protection, required reviews, and repository access control.
- Whether OpenAI is actually configured in any deployment.

**Methodological limitations:**

- **No DAST.** No live instance was scanned. Dynamic verification ran in-process against
  `create_app()` with stub services — which produced stronger evidence for the findings raised, but
  would not surface issues arising from real Uvicorn/proxy/TLS interaction, HTTP request smuggling
  between an upstream proxy and the application, or genuine concurrency behaviour under load.
- **No load or stress testing**, per the engagement constraints. The DoS findings (SEC-002 through
  SEC-005) are supported by measured single-request CPU and memory cost plus code analysis, not by
  demonstrated service degradation.
- **No third-party testing.** OpenAI API behaviour was inferred from the adapter code and public
  documentation.
- **Static reachability limits.** Conclusions about unreachable code paths (notably SEC-023) rest on
  reading build configuration; runtime verification against a built image would be stronger.
- **Point-in-time.** This reflects commit `27b1d16` on 2026-09-12. Dependency advisories in particular
  change continuously — which is precisely why SEC-015 recommends automating the scans performed here.
- **Severity depends on deployment.** Ratings assume the supported production path (Helm chart,
  `auth_mode=api_key`). A deployment inheriting the Compose defaults is materially worse: SEC-001
  through SEC-006 all escalate.

**What this audit does cover with high confidence:** the complete application source, every migration,
every Helm template, all Terraform, the CI workflow, the full dependency tree, and the entire Git
history. Every finding raised is anchored to a specific file and line, and all but one was reproduced
against the running code.

---

## 32. Final Security Assessment

### Top 5 risks

1. **SEC-001 — Tenant isolation is caller-asserted.** One shared key plus a caller-chosen header means
   any integrating client can read, replay and evaluate any other tenant's data. This is the finding
   that would most damage the operator if exploited, and it invalidates the multi-tenant promise the
   data model makes.
2. **SEC-003 — Unmetered access to paid inference.** An endpoint with no client identity that drives
   200 provider calls per request, with no cap anywhere in the path, and a Helm chart that puts the
   provider key in the API pod. Direct, uncapped financial loss.
3. **SEC-002 — Caller-supplied regex on the API event loop.** Measured at three seconds of blocking
   CPU for a 26-character input, doubling per character, with 32 rules permitted per request. A
   complete API-tier denial of service from a single well-formed JSON body.
4. **SEC-004 — Caller-authored retry policy.** The "immutable policy snapshot" — presented as a
   safety contract — is written by the caller. One request can schedule five million attempts, each
   consuming a database transaction, an outbox row, and a broker message.
5. **SEC-008 + SEC-012 — Insecure defaults and over-distributed secrets.** The shipped default is
   unauthenticated, and every workload receives every credential. Neither is exploitable alone; both
   substantially amplify everything above.

### Top 5 remediations

1. **Bind identity to the credential.** A credential table mapping digest → `client_id`, resolved in
   `authenticate_api_key` and propagated through `request.state`. This single change fixes SEC-001,
   fixes SEC-006, and provides the foundation for per-tenant quotas. **Effort: M. Highest leverage in
   the entire report.**
2. **Replace the backtracking regex engine and move evaluation to a worker.** RE2 for linear-time
   matching; emit an outbox event instead of computing inline. Fixes SEC-002 and aligns the code with
   ADR-0002. **Effort: S + M.**
3. **Give `policy` a bounded schema.** A Pydantic model with `extra="forbid"` and explicit `ge`/`le`
   bounds, plus server-side clamping. Fixes SEC-004 and the mass-assignment gap in one change.
   **Effort: S.**
4. **Require identity and metering on every `/v1/` route.** Make the rate-limit guard unconditional,
   add the missing identity to the regression endpoint, and add a route-table test asserting no `/v1/`
   path is anonymous. Fixes SEC-003; prevents the class from recurring. **Effort: S.**
5. **Fix the deployment defaults.** Invert `auth_mode`, split the Secret, add `securityContext` and
   disable token automounting. Four small changes across `settings.py` and two Helm templates.
   Fixes SEC-008, SEC-012 and SEC-011. **Effort: S.**

### Production readiness

**Are there blockers? Yes.**

SEC-001 and SEC-003 are production blockers for any deployment serving more than one tenant or holding
a provider credential:

- **SEC-001** because the system presents a multi-tenant data model whose isolation does not hold. A
  customer discovering they can read another customer's data is not a bug report; it is an incident.
- **SEC-003** because the financial exposure is unbounded and requires no privilege beyond the
  credential every integrator already holds.

SEC-002 and SEC-004 are blockers for any availability commitment, since either allows a single caller
to degrade service for all others.

A **single-tenant internal deployment** behind an authenticated gateway, with no provider key in the
API pod, is a materially different risk picture — SEC-001 largely collapses, since there is only one
tenant to isolate. Even then, SEC-002 and SEC-004 should be fixed before load-bearing use.

### Assessment

> ## **Ready with Conditions**

**Justification.**

"Not Recommended for Production" would misrepresent this codebase. Its foundations are genuinely
sound: no injection, no unsafe deserialization, no SSRF, no custom crypto, a clean dependency tree, a
clean Git history, correct transactional semantics, a well-designed provider boundary, and 32
identified security controls implemented deliberately and correctly. The engineering judgment on
display is good. Zero Semgrep findings across 310 rules is not luck.

"Ready" would be equally wrong. Four High findings are reachable by an ordinary authenticated caller,
two of them are production blockers in a multi-tenant deployment, and there is currently no test
anywhere in the suite that would catch a cross-tenant access regression.

The honest characterization is that the **reliability** engineering is finished and the **trust-model**
engineering is not. The system was designed against failure — outages, crashes, duplicate delivery,
partial writes — and it handles all of those well. It was not yet designed against a hostile caller.
That is a defensible V0 scope decision, and the project documents its boundary honestly in
`SECURITY.md` and ADR-0004. It is simply not a finished one.

**Conditions for production release:**

1. SEC-001 resolved — authorization anchored to the credential, with the tenant-isolation test suite
   from §29 passing.
2. SEC-003 resolved — no `/v1/` route reachable without an identified, rate-limited caller.
3. SEC-002 and SEC-004 resolved — no caller-supplied input can consume unbounded CPU or schedule
   unbounded work.
4. SEC-008 and SEC-012 resolved — authentication on by default; the provider key present only in the
   worker.
5. The §29 authorization tests merged and running in CI.
6. The `security` CI job from SEC-015 running and gating merges.

With those six conditions met, the remaining findings are ordinary security debt appropriate to the
NEXT and LATER tracks, and this system would be in better shape than most services of comparable
maturity.

---

## Appendix A — Remediation Backlog

| Priority | Finding | Severity | Recommended Fix | Effort | Suggested Order |
| -------- | ------- | -------- | --------------- | ------ | --------------- |
| NOW | SEC-001 | High | Credential→identity binding; drop `X-Client-Id` as an authorization input | M | 1 |
| NOW | SEC-002 | High | RE2 or safe-pattern allowlist; bound schema complexity; move evaluation to a worker | S→M | 2 |
| NOW | SEC-003 | High | Require identity; unconditional rate-limit guard; lower the case cap | S | 3 |
| NOW | SEC-004 | High | Bounded `RunPolicyRequest` model; server-side clamping; backoff floor | S | 4 |
| NOW | SEC-008 | Medium | Default `auth_mode` to `api_key`; refuse `disabled` outside local | XS | 5 |
| NOW | SEC-012 | Medium | Split the Secret; provider key to the worker only | S | 6 |
| NOW | SEC-005 | Medium | Count bytes read; fail closed on a malformed `Content-Length` | S | 7 |
| NOW | SEC-006 | Medium | Key the limiter on the credential; atomic `SET NX` + `INCR` | XS | 8 |
| NEXT | SEC-007 | Medium | Validate and truncate `client_id`; define the audit failure policy; add a metric | S | 9 |
| NEXT | SEC-011 | Medium | `securityContext` + `automountServiceAccountToken: false` | S | 10 |
| NEXT | SEC-010 | Medium | KDF with salt/pepper; truncated audit fingerprint; entropy requirement | M | 11 |
| NEXT | SEC-020 | Medium | Wire the provider timeout; add a config-coverage test | XS | 12 |
| NEXT | SEC-009 | Low | Disable docs in `api_key` mode; allowlist public paths | XS | 13 |
| NEXT | SEC-015 | Low | SHA-pin actions; add SCA/SAST/secret/container scanning | S | 14 |
| NEXT | SEC-017 | Low | `CLIENT_ID_PATTERN`; handle `DataError` as `422` | XS | 15 |
| NEXT | — | — | Pagination on the three history endpoints | S | 16 |
| NEXT | — | — | Per-client run and provider-call quotas | M | 17 |
| NEXT | — | — | Security metrics and alert rules | S | 18 |
| LATER | SEC-019 | Low | PSA labels, default-deny NetworkPolicy, LimitRange | S | 19 |
| LATER | SEC-018 | Low | Separate `arr_migrator` / `arr_runtime` database roles | M | 20 |
| LATER | SEC-016 | Low | Digest-pin base image; deploy by digest; `HEALTHCHECK`; SBOM | S | 21 |
| LATER | SEC-013 | Low | Sanitized exception context in the log formatter | XS | 22 |
| LATER | SEC-014 | Low | Validate `traceparent`; cap `tracestate`; define the trust policy | XS | 23 |
| LATER | SEC-022 | Low | Opt-in echo; encryption at rest; retention; crypto-shredding erasure | L | 24 |
| LATER | SEC-021 | Info | Loopback-bind Compose ports; Grafana admin password | XS | 25 |
| LATER | SEC-023 | Info | Upgrade pytest to ≥ 9.0.3 | XS | 26 |
| LATER | — | — | Audit logging for sensitive reads | S | 27 |
| LATER | — | — | Enforce `rediss://`/`amqps://` in `api_key` mode | XS | 28 |
| LATER | — | — | SLSA provenance and artifact signing | M | 29 |
| LATER | — | — | `PodDisruptionBudget`; better background-process probes | XS | 30 |
| LATER | — | — | Remove checked-in Terraform provider binaries | XS | 31 |

**Effort key:** XS < 1 hour · S < 1 day · M 1–3 days · L > 3 days

**Estimated total for the NOW track:** approximately 5–8 engineering days, dominated by SEC-001
(credential model) and SEC-002 (moving evaluation to the worker).

---

## Appendix B — Verification Evidence Index

Every finding is anchored to a reproducible in-process experiment. The harnesses ran against
`create_app()` with stub services — no network, no database, no broker, and no modification to
repository source.

| Exp. | What it establishes | Finding | Result |
| ---- | ------------------- | ------- | ------ |
| V1 | Caller regex → 2.99 s blocking CPU at 26 chars, doubling per character | SEC-002 | Confirmed |
| V2 | Same via `json_schema` `pattern` | SEC-002 | Confirmed |
| V3 | Remote `$ref` raises `Unresolvable`; no outbound fetch | — | **Refuted** |
| V4 | `max_attempts: 5000000` and unknown keys persist into the snapshot | SEC-004 | Confirmed |
| V5 | One credential reads as two different tenants | SEC-001 | Confirmed |
| V6 | One credential produces five distinct rate-limit buckets | SEC-006 | Confirmed |
| V7 | Regression endpoint: `200`, no limiter call, 200 provider executions | SEC-003 | Confirmed |
| V8 | Declared `Content-Length` → `413`; chunked → `202` | SEC-005 | Confirmed |
| V9 | Failing audit sink; `403` returned with zero records persisted | SEC-007 | Confirmed |
| V10 | `/openapi.json`, `/docs`, `/redoc` all `200` unauthenticated | SEC-009 | Confirmed |
| V11 | Stored `credential_fingerprint` is byte-identical to the configured verifier | SEC-010 | Confirmed |
| V12 | Attacker-chosen `traceparent` and 200-char `tracestate` accepted | SEC-014 | Confirmed |
| V13 | 8 MiB chunked body → 145.7 MiB peak RSS against a 1 KiB limit | SEC-005 | Confirmed |
| V14 | Retry policy permits 5,000,000 attempts with sub-millisecond backoff | SEC-004 | Confirmed |
| V15 | Idempotency uniqueness is scoped by the caller-asserted client id | SEC-001 | Confirmed |
| V16 | `evaluate()` calls `evaluate_rules()` inline — no thread offload, no timeout | SEC-002 | Confirmed |
| V17 | 2,000 failed authentications in 0.001 s; `hmac.compare_digest` in use | — | **Refuted** |

**15 confirmed, 2 refuted.** (V15 and V16 are code-structure confirmations supporting SEC-001 and
SEC-002 rather than independent findings.)

---

*End of report. Prepared for the repository owner under an authorized security review engagement.
No source code was modified. No production system was contacted. No destructive test was performed.*
