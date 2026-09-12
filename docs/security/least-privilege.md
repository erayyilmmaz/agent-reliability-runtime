# Least privilege across secrets, Kubernetes and the database

SEC-E4 narrows what each process holds and what it is allowed to do, so that a
single compromised pod does not expose every credential in the system.

Covers SEC-012, SEC-011, SEC-019, SEC-018, SEC-TRAN-01 and SEC-OPS-01.

---

## 1. Secret split by role (SEC-012)

Previously every workload mounted one Secret via `envFrom`, so the API,
dispatcher and scheduler all received `APP_OPENAI_API_KEY` even though none of
them calls a provider. That is also what turned the regression endpoint's
resource problem into a financial one.

The chart now expects up to three Secrets:

| Secret | Contents | Mounted into |
| ------ | -------- | ------------ |
| `existingSecret.common` | `APP_DATABASE_URL`, `APP_REDIS_URL`, `APP_RABBITMQ_URL`, `APP_AUTH_CREDENTIALS`, `APP_AUTH_PEPPER`, `APP_PAYLOAD_KEK` | api, worker, dispatcher, scheduler |
| `existingSecret.provider` | `APP_OPENAI_API_KEY` | **worker only** |
| `existingSecret.migration` | `APP_MIGRATION_DATABASE_URL` | migration Job only (falls back to `common` when empty) |

```bash
kubectl create secret generic agent-reliability-runtime-provider \
  --namespace arr-production \
  --from-literal=APP_OPENAI_API_KEY="$(cat provider-key.txt)"
```

Verify after deploying:

```bash
kubectl exec deploy/arr-api        -- printenv | grep -c OPENAI   # 0
kubectl exec deploy/arr-dispatcher -- printenv | grep -c OPENAI   # 0
kubectl exec deploy/arr-scheduler  -- printenv | grep -c OPENAI   # 0
kubectl exec deploy/arr-worker     -- printenv | grep -c OPENAI   # 1
```

> **Breaking change.** `existingSecret.name` became `existingSecret.common`.
> The chart version is now `0.2.0`.

---

## 2. Pod hardening (SEC-011)

Every pod the chart renders — including the migration Job — now sets:

```yaml
automountServiceAccountToken: false
securityContext:                 # pod
  runAsNonRoot: true
  runAsUser: 10001
  runAsGroup: 10001
  fsGroup: 10001
  seccompProfile: { type: RuntimeDefault }
securityContext:                 # container
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities: { drop: [ALL] }
```

No process in this runtime calls the Kubernetes API, so no pod needs a
service-account token; leaving one mounted only gives an attacker with code
execution a path to the cluster API.

`runAsUser: 10001` matches the UID pinned in `docker/Dockerfile`. The two must
stay in sync — the chart asserts an identity the image has to actually provide.

`readOnlyRootFilesystem: true` requires a writable `/tmp`, provided as an
`emptyDir` and used for heartbeats and Python temporary files.

---

## 3. Namespace guardrails (SEC-019)

The Terraform module now applies, per environment:

- **Pod Security Admission** labels at `restricted`, so a workload cannot opt
  out of the controls above by simply omitting them. Enforce, audit and warn are
  all set.
- **Default-deny ingress**, plus an explicit same-namespace allow (the runtime
  reaches PostgreSQL, Redis and RabbitMQ in-namespace) and an optional rule
  admitting the ingress controller's namespace to the API port only.
- **LimitRange**, so one container cannot claim the whole namespace quota.

```hcl
module "runtime_namespace" {
  source                  = "../../modules/runtime_namespace"
  namespace               = var.namespace
  resource_quota_hard     = var.resource_quota_hard
  pod_security_standard   = "restricted"
  ingress_namespace_label = "ingress-nginx"
}
```

**Sequencing matters.** `restricted` PSA rejects pods that lack the SEC-011
settings. Deploy the chart at `0.2.0` first, confirm pods start, then enable PSA
enforcement. Setting `pod_security_standard = ""` skips the labels during a
migration window.

---

## 4. Database role separation (SEC-018)

The append-only triggers on `run_events`, `run_attempts` and
`security_audit_events` are enforced by PostgreSQL rather than by application
code, which is the right design. But a table's owner can run
`ALTER TABLE ... DISABLE TRIGGER ALL`. While the API runs as the schema owner,
those triggers protect against application bugs, not against a compromised
application.

Audit retention does not use that escape hatch. `security_audit_events` needs a
way to age rows out (PERF-005), and disabling the trigger would both stall the
request path — it takes `ShareRowExclusiveLock`, measured blocking a concurrent
audit `INSERT` for 4.0 s — and open deletes to *every* session for as long as it
is off. Instead the trigger admits a `DELETE` only from a transaction that has
set `arr.allow_audit_purge = 'on'` via `SET LOCAL`, which expires with that one
transaction. `UPDATE` is never admitted. The gate is not the boundary: the
boundary is that `arr_runtime` holds `INSERT` only and cannot delete whatever it
sets. What the gate adds is that the owner cannot erase audit history by
accident. See `src/agent_runtime/security/audit_retention.py`.

`scripts/sql/roles.sql` creates two roles:

| Role | Used by | Capabilities |
| ---- | ------- | ------------ |
| `arr_migrator` | migration Job | Owns the schema; DDL |
| `arr_runtime` | api, worker, dispatcher, scheduler | DML only; `INSERT`-only on append-only tables; no `ALTER`, no ownership |

```bash
psql "$ADMIN_URL" \
  -v migrator_password="$(openssl rand -base64 32)" \
  -v runtime_password="$(openssl rand -base64 32)" \
  -f scripts/sql/roles.sql
```

Then set `APP_MIGRATION_DATABASE_URL` (migrator) in the migration Secret and
`APP_DATABASE_URL` (runtime) in the common Secret. `migrations/env.py` prefers
the migration URL and falls back to `APP_DATABASE_URL`, so an existing
single-role deployment keeps working until the split is applied.

Verify:

```sql
-- as arr_runtime, all three must fail
ALTER TABLE security_audit_events DISABLE TRIGGER ALL;
DELETE FROM run_events;
UPDATE security_audit_events SET outcome = 'ALLOWED';
```

---

## 5. Transport encryption (SEC-TRAN-01)

`staging` and `production` reject `redis://` and `amqp://` at startup. Use
`rediss://` and `amqps://`.

The escape hatch is deliberate and named:

```bash
APP_ALLOW_PLAINTEXT_TRANSPORT=true
```

It exists so an in-cluster kind/k3d demo stays possible. Setting it is a
recorded decision; the point is that a plaintext broker link in a deployed
environment can no longer happen by accident.

The check runs after credential validation, so a missing credential registry —
the more fundamental misconfiguration — is still the first error an operator
sees.

---

## 6. Meaningful background probes (SEC-OPS-01)

The dispatcher, worker and scheduler serve no HTTP traffic, and their probes
used to run `kill -0 1`. That passes for a deadlocked process, and for a worker
whose broker connection has silently dropped.

Each background process now writes a heartbeat after real progress:

| Process | Heartbeat written when |
| ------- | ---------------------- |
| dispatcher | an outbox dispatch pass completes |
| scheduler | a lease-recovery and retry-scheduling pass completes |
| worker | the AMQP connection is confirmed open (event-driven, so loop progress is not a liveness signal) |

Probes read it:

```yaml
livenessProbe:
  exec:
    command: [agent-runtime-healthcheck, --max-age, "60"]
```

The file is written atomically via `os.replace`, so a probe never reads a
partial heartbeat, and a write failure never propagates into the work loop — a
stale file and a restarted pod is the correct outcome, not a crashed process.

`podDisruptionBudget` is created **only** for workloads with more than one
replica. A `minAvailable: 1` budget on a single-replica Deployment makes its pod
permanently un-evictable and blocks every node drain.

---

## 7. Regression gate

`scripts/helm-security-check.sh` renders the chart and fails if any of the above
regresses. It runs in CI in the `helm-lint` job and asserts:

- every workload disables service-account token automounting
- `runAsNonRoot`, `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem`,
  `seccompProfile: RuntimeDefault` and `capabilities.drop: [ALL]` on all pods
- the provider Secret is referenced exactly once, by the worker
- no probe uses `kill -0 1`, and background workloads use heartbeat probes

Both failure modes were verified by deliberately weakening the chart and
confirming the gate fails.
