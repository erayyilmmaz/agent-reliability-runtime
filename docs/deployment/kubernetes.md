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
