# Kubernetes and Helm deployment

The Helm chart deploys only Agent Reliability Runtime application processes:
the API, worker, transactional-outbox dispatcher, scheduler/recovery process,
and the pre-install/pre-upgrade Alembic migration Job. PostgreSQL, Redis and
RabbitMQ are external production dependencies. The included
[`local-dependencies.yaml`](../../deploy/kubernetes/local-dependencies.yaml)
is only a disposable kind/k3d development option.

## Security contract

The chart creates a ConfigMap only for non-secret `APP_*` settings. It never
renders a Kubernetes `Secret` or embeds credentials. Before installing, create
the Secret named by `existingSecret.name`; it must contain the application
variables below:

- `APP_DATABASE_URL`, `APP_REDIS_URL`, `APP_RABBITMQ_URL`
- `APP_AUTH_CREDENTIALS` and `APP_AUTH_PEPPER` when `config.authMode=api_key`
- optionally `APP_OPENAI_API_KEY`

For the supplied local dependencies it additionally needs `POSTGRES_DB`,
`POSTGRES_USER`, `POSTGRES_PASSWORD`, `RABBITMQ_DEFAULT_USER`, and
`RABBITMQ_DEFAULT_PASS`. Use External Secrets or your cloud secret manager in
production. Do not commit a values file containing any of these values.

## kind local deployment

Prerequisites are Docker, `kubectl`, Helm 3, and kind. The commands below use
shell variables so the values stay out of manifests. Use non-demo values in a
real environment and avoid sharing your shell history.

```bash
kind create cluster --name arr
docker build -t agent-reliability-runtime:dev -f docker/Dockerfile .
kind load docker-image agent-reliability-runtime:dev --name arr
kubectl create namespace arr

export ARR_POSTGRES_PASSWORD='replace-local-postgres-password'
export ARR_RABBITMQ_PASSWORD='replace-local-rabbitmq-password'
# Follow docs/security/credentials.md to provision APP_AUTH_CREDENTIALS and
# APP_AUTH_PEPPER in this shell. Deliver the generated API key to the caller.

kubectl -n arr create secret generic agent-reliability-runtime-secrets \
  --from-literal=POSTGRES_DB=agent_runtime \
  --from-literal=POSTGRES_USER=runtime \
  --from-literal=POSTGRES_PASSWORD="$ARR_POSTGRES_PASSWORD" \
  --from-literal=RABBITMQ_DEFAULT_USER=runtime \
  --from-literal=RABBITMQ_DEFAULT_PASS="$ARR_RABBITMQ_PASSWORD" \
  --from-literal=APP_DATABASE_URL="postgresql+asyncpg://runtime:$ARR_POSTGRES_PASSWORD@postgres:5432/agent_runtime" \
  --from-literal=APP_REDIS_URL=redis://redis:6379/0 \
  --from-literal=APP_RABBITMQ_URL="amqp://runtime:$ARR_RABBITMQ_PASSWORD@rabbitmq:5672/" \
  --from-literal=APP_AUTH_CREDENTIALS="$APP_AUTH_CREDENTIALS" \
  --from-literal=APP_AUTH_PEPPER="$APP_AUTH_PEPPER"

kubectl -n arr apply -f deploy/kubernetes/local-dependencies.yaml
kubectl -n arr rollout status deployment/postgres --timeout=180s
kubectl -n arr rollout status deployment/redis --timeout=180s
kubectl -n arr rollout status deployment/rabbitmq --timeout=180s

helm upgrade --install arr charts/agent-reliability-runtime \
  --namespace arr \
  --wait --wait-for-jobs --timeout 5m \
  --set image.repository=agent-reliability-runtime \
  --set image.tag=dev \
  --set image.pullPolicy=IfNotPresent \
  --set existingSecret.name=agent-reliability-runtime-secrets
```

Verify the independently scalable application deployments and API probes:

```bash
kubectl -n arr get deployments,pods,jobs
kubectl -n arr scale deployment/arr-agent-reliability-runtime-worker --replicas=3
kubectl -n arr rollout status deployment/arr-agent-reliability-runtime-worker
kubectl -n arr port-forward service/arr-agent-reliability-runtime-api 8000:8000
curl http://localhost:8000/healthz
```

In another terminal, submit a deterministic run using `X-API-Key` with the
raw API key delivered by credential provisioning.
Set `worker.autoscaling.enabled=true` where a metrics-server is available to
install the worker HPA. The API is intentionally a separate deployment and is
not affected by worker scaling.

Remove the local demonstration environment when finished:

```bash
helm -n arr uninstall arr
kind delete cluster --name arr
```

## k3d alternative

Create a local registry-backed cluster and import the same image:

```bash
k3d cluster create arr
k3d image import agent-reliability-runtime:dev -c arr
```

Then use the same namespace, Secret, local dependency, Helm install, and
verification commands above. For a remote registry, set `image.repository`,
`image.tag`, and `imagePullSecrets` in a non-secret deployment values file.


## Scaling the API (PERF-010 / PERF-009)

One API pod runs one uvicorn process on one asyncio event loop, so it saturates
at **one CPU core** — measured at ~103% CPU and ~450-500 RPS per pod. Giving a
pod more CPU does nothing; capacity comes from replicas.

The chart therefore sets `api.resources.requests == limits == 1` CPU
(Guaranteed QoS) and ships an API `HorizontalPodAutoscaler`, disabled by
default.

### Before enabling the autoscaler: check the connection ceiling

Every process opens its own PostgreSQL pool. The cluster-wide ceiling is:

```
(api + worker + dispatcher + scheduler replicas) × (dbPoolSize + dbMaxOverflow)
    must stay below PostgreSQL max_connections
```

With the shipped defaults:

| Scenario | Arithmetic | Total | Default `max_connections` 100 |
| -------- | ---------- | ----: | ----------------------------- |
| No autoscaling | `(2+2+1+1) × 10` | 60 | Fits |
| API autoscaler at max | `(4+2+1+1) × 10` | 80 | Fits |
| **Both autoscalers at max** | `(4+10+1+1) × 10` | **160** | **Exceeds — do not enable as-is** |

To run both autoscalers, do one of:

- raise `max_connections` on the database (and size its memory accordingly),
- lower `config.dbPoolSize` / `config.dbMaxOverflow` — measured need is 1-2
  active connections per API pod at 400+ RPS, so there is room, or
- put PgBouncer in transaction-pooling mode between the runtime and PostgreSQL.

```bash
helm upgrade --install arr charts/agent-reliability-runtime \
  --set api.autoscaling.enabled=true \
  --set api.autoscaling.maxReplicas=4 \
  --set config.dbPoolSize=5 \
  --set config.dbMaxOverflow=5
```

Verify after rollout:

```bash
kubectl get hpa
psql "$ADMIN_URL" -c \
  "SELECT count(*), max_conn FROM pg_stat_activity,
   (SELECT setting::int AS max_conn FROM pg_settings WHERE name='max_connections') s
   GROUP BY max_conn;"
```

## Admission ceilings are also capacity ceilings (PERF-008)

The limits in `docs/security/resource-controls.md` are a security control and
are working as designed. They are repeated here because they *also* decide how
much of a pod's measured capacity any one caller can reach, and the two
readings need reconciling before an incident does it for you.

| Setting (`APP_` prefix) | Default | Hard bound | What it caps per window |
| ----------------------- | ------: | ---------: | ----------------------- |
| `RATE_LIMIT_REQUESTS` / `RATE_LIMIT_WINDOW_SECONDS` | 60 / 60 s | ≤ 10,000 / ≥ 1 s | HTTP requests **per principal** |
| `PRINCIPAL_PROVIDER_CALLS` | 120 | ≤ 10,000 | Provider calls per principal |
| `TENANT_PROVIDER_CALLS` | 240 | ≤ 20,000 | Provider calls per tenant |
| `QUOTA_WINDOW_SECONDS` | 3600 s | 60-86,400 s | The window both call quotas use |

The hard bounds are what an operator may configure. They exist so a
misconfiguration cannot switch admission control off; they are not tuning
targets.

**The default rate limit is one request per second, per principal.** 60
requests over a 60 s fixed window. A pod serves ~450-500 RPS, so at defaults a
single caller reaches well under 1% of one pod, and the measured throughput
figures in `docs/performance-audit.md` are only reachable because
`performance-tests/docker-compose.perf.yml` raises the ceiling
(`APP_RATE_LIMIT_REQUESTS=10000`). If a production integration is expected to
sustain more than 1 RPS, that limit — not capacity — is what it will hit first,
and it fails as `429`, not as latency.

Because the window is fixed rather than rolling, a caller can spend the whole
allowance at the end of one window and again at the start of the next: size for
`2 × limit` arriving back to back, not for `limit / window` as a smooth rate.

**Reconciling provider calls with the commercial budget.** Quotas cap calls, not
currency, and they are per principal and per tenant — never global. The
cluster-wide worst case is:

```
principals × PRINCIPAL_PROVIDER_CALLS   (bounded again per tenant by
                                         tenants × TENANT_PROVIDER_CALLS)
    calls per QUOTA_WINDOW_SECONDS
```

At defaults that is 120 calls/hour per principal and 240/hour per tenant. Ten
tenants of two principals each is up to 2,400 calls/hour, and nothing in the
runtime stops the eleventh tenant from adding 240 more. Set these so the sum
across the tenants you have provisioned stays inside the spend limit on the
provider account, and treat the provider account's own cap as the real backstop
— reservations are conservative and are not refunded on failure, but they are
also not billing.

Since PERF-004 the provider-call budget is checked at admission, so exhaustion
returns `429` at submit instead of consuming the full pipeline and failing at
the worker. That changes the failure into a cheap one; it does not raise the
ceiling.

Every role must receive the same limits — the API rejects at admission and the
worker reserves at execution, and they disagree if configured differently.
