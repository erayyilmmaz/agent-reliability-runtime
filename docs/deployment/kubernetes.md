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
