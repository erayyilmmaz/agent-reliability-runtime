.DEFAULT_GOAL := help

.PHONY: help dev infra test lint typecheck format-check migrate down

help:
	@echo "make dev          Build and start the local runtime and infrastructure"
	@echo "make infra        Start only PostgreSQL, Redis, and RabbitMQ"
	@echo "make test         Run the deterministic test suite"
	@echo "make lint         Run Ruff and MyPy"
	@echo "make format-check Check Ruff formatting"
	@echo "make migrate      Apply Alembic migrations through the API image"
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

migrate:
	docker compose run --rm api alembic upgrade head

down:
	docker compose down --remove-orphans
