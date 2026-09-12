# Local development surface

SEC-E6 keeps the Compose stack from being reachable outside the machine running
it, and removes its default Grafana credential.

Covers SEC-021 and SEC-021B.

---

## Why this is Informational, not a defect

The Compose stack runs with `APP_AUTH_MODE=disabled` and well-known local
credentials (`runtime:runtime`), and that is a deliberate, documented choice —
it is what makes `make smoke` a genuinely credentials-free five-minute demo.
`SECURITY.md` has always said so, and the Helm chart, which defines the
production path, defaults to `api_key`.

The residual risk was never the weak local passwords. It was **reachability**:
every port was published on all interfaces, so running `make dev` on a café or
office network exposed PostgreSQL, Redis, RabbitMQ and an unauthenticated API
to everyone on that network.

---

## 1. Loopback binding (SEC-021)

All nine published ports now bind to `127.0.0.1`:

| Service | Port |
| ------- | ---- |
| api | 8000 |
| postgres | 5432 |
| redis | 6379 |
| rabbitmq | 5672, 15672 |
| otel-collector | 4318, 8889 |
| prometheus | 9090 |
| grafana | 3000 |

Nothing about the developer experience changes: `http://localhost:8000`,
`http://localhost:3000` and the smoke test all work exactly as before.

LAN exposure is now an explicit decision:

```bash
ARR_BIND_HOST=0.0.0.0 make dev
```

Verify:

```bash
docker compose config | grep -A1 published    # every entry: host_ip: 127.0.0.1
```

---

## 2. Grafana credentials (SEC-021B)

Grafana previously started with its built-in `admin:admin`. It now requires an
admin password and will not start without one:

```yaml
GF_SECURITY_ADMIN_USER: ${GRAFANA_ADMIN_USER:-arr-admin}
GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD:?set GRAFANA_ADMIN_PASSWORD in .env}
GF_AUTH_ANONYMOUS_ENABLED: "false"
GF_USERS_ALLOW_SIGN_UP: "false"
```

There is deliberately **no default value**, in `.env.example` or anywhere else.
A shipped default is the thing being fixed; copying `.env.example` must not
hand you a working password that everyone else also has.

Set one:

```bash
make grafana-password       # appends a generated value to .env
```

or by hand:

```bash
echo "GRAFANA_ADMIN_PASSWORD=$(openssl rand -base64 24)" >> .env
```

Without it, `make dev` fails immediately with:

```
required variable GRAFANA_ADMIN_PASSWORD is missing a value: set GRAFANA_ADMIN_PASSWORD in .env
```

That is the intended behaviour — fail closed, with the fix named in the error.

### Keeping the smoke demo credentials-free

`scripts/compose-smoke.sh` generates a throwaway password for its own run:

```bash
export GRAFANA_ADMIN_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 24)}"
```

The stack is torn down by the script's `trap`, so the value never outlives the
run. `make smoke` and CI still need no credentials from the operator, and the
README's five-minute demo promise still holds.

`GF_SECURITY_COOKIE_SECURE` stays `false` because local Grafana is served over
plain HTTP; setting it would break the session cookie on `http://localhost:3000`.

---

## 3. What is deliberately unchanged

- **`runtime:runtime` for PostgreSQL and RabbitMQ.** These are now only
  reachable from the host itself. Rotating them would add friction to the demo
  without changing the exposure.
- **`APP_AUTH_MODE=disabled`.** This is what makes the demo credentials-free,
  and `Settings` already refuses `disabled` outside `environment=local`
  (SEC-008), so it cannot leak into a deployed environment.
- **Redis without a password.** Loopback-only, and the rate limiter holds no
  sensitive data.

The stack also carries a header comment now, so the warning travels with the
file rather than living only in `SECURITY.md`.
