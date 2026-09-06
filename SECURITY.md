# Security policy

## Supported version

Only the current `main` branch is supported during V0 development.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability or attach secrets,
prompts, provider responses, or production data. Contact the repository owner
privately with a minimal reproduction, affected revision, impact, and safe
evidence. The maintainer will acknowledge receipt, assess impact, and agree on
a remediation and disclosure timeline.

## Operational baseline

Production deployments must enable `APP_AUTH_MODE=api_key`, provide only the
API key's SHA-256 digest through a secret manager, and restrict database,
Redis, RabbitMQ, Grafana, and Prometheus network access. Local Compose is a
development demo and intentionally uses disabled authentication.
