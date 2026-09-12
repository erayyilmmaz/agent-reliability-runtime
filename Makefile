.DEFAULT_GOAL := help

.PHONY: help dev infra test lint typecheck format-check coverage migrate smoke recruiter-demo helm-lint terraform-fmt terraform-validate grafana-password down

help:
	@echo "make dev          Build and start the local runtime and infrastructure"
	@echo "make infra        Start only PostgreSQL, Redis, and RabbitMQ"
	@echo "make test         Run the deterministic test suite"
	@echo "make lint         Run Ruff and MyPy"
	@echo "make format-check Check Ruff formatting"
	@echo "make coverage     Produce terminal coverage for the runtime package"
	@echo "make migrate      Apply Alembic migrations through the API image"
	@echo "make smoke        Verify the full Docker Compose deterministic demo"
	@echo "make recruiter-demo Run the Python SDK recruiter tour against make dev"
	@echo "make helm-lint    Lint and render the Helm chart"
	@echo "make terraform-fmt Check Terraform formatting"
	@echo "make terraform-validate Initialize without state and validate every Terraform environment"
	@echo "make grafana-password  Append a generated Grafana admin password to .env"
	@echo "make down         Stop and remove local containers"

dev:
	docker compose up --build

infra:
	docker compose up -d postgres redis rabbitmq

test:
	uv run pytest

lint:
	uv run ruff check src tests
	uv run mypy src

format-check:
	uv run ruff format --check src tests

coverage:
	uv run pytest --cov=agent_runtime --cov-report=term-missing

migrate:
	docker compose run --rm api alembic upgrade head

smoke:
	bash scripts/compose-smoke.sh

recruiter-demo:
	uv run python examples/recruiter_demo.py

helm-lint:
	helm lint charts/agent-reliability-runtime
	helm template arr charts/agent-reliability-runtime --namespace arr

terraform-fmt:
	terraform fmt -check -recursive infra/terraform

terraform-validate:
	terraform -chdir=infra/terraform/environments/sandbox init -backend=false -input=false
	terraform -chdir=infra/terraform/environments/sandbox validate
	terraform -chdir=infra/terraform/environments/staging init -backend=false -input=false
	terraform -chdir=infra/terraform/environments/staging validate
	terraform -chdir=infra/terraform/environments/production init -backend=false -input=false
	terraform -chdir=infra/terraform/environments/production validate

grafana-password:
	@grep -q '^GRAFANA_ADMIN_PASSWORD=.\+' .env 2>/dev/null \
		&& echo 'GRAFANA_ADMIN_PASSWORD already set in .env' \
		|| { echo "GRAFANA_ADMIN_PASSWORD=$$(openssl rand -base64 24)" >> .env; \
		     echo 'Generated GRAFANA_ADMIN_PASSWORD in .env'; }

down:
	docker compose down --remove-orphans
