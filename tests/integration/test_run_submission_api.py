from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient

from agent_runtime.api.main import create_app
from agent_runtime.application.runs import (
    AttemptSnapshot,
    EventSnapshot,
    IdempotencyConflictError,
    RunNotFoundError,
    RunSnapshot,
    canonical_request_hash,
)
from agent_runtime.domain.states import EvaluationStatus, ExecutionStatus
from agent_runtime.settings import Settings

HEADERS = {"Idempotency-Key": "create-run-0001", "X-Client-Id": "test-client"}


class InMemoryRunService:
    """Thread-safe test double for API contract tests; PostgreSQL is tested live separately."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs_by_key: dict[tuple[str, str], tuple[str, RunSnapshot]] = {}
        self._runs: dict[uuid.UUID, RunSnapshot] = {}
        self.submission_count = 0

    async def submit(
        self,
        *,
        client_id: str,
        idempotency_key: str,
        input_payload: dict[str, Any],
        policy_snapshot: dict[str, Any],
    ) -> tuple[RunSnapshot, bool]:
        request_hash = canonical_request_hash(input_payload, policy_snapshot)
        key = (client_id, idempotency_key)
        with self._lock:
            existing = self._runs_by_key.get(key)
            if existing is not None:
                existing_hash, run = existing
                if existing_hash != request_hash:
                    raise IdempotencyConflictError(
                        "Idempotency-Key is already bound to a different request"
                    )
                return run, True

            run = RunSnapshot(
                id=uuid.uuid4(),
                execution_status=ExecutionStatus.QUEUED,
                evaluation_status=EvaluationStatus.NOT_RUN,
                created_at=datetime.now(UTC),
                started_at=None,
                completed_at=None,
                replay_of_run_id=None,
                error_code=None,
            )
            self._runs_by_key[key] = (request_hash, run)
            self._runs[run.id] = run
            self.submission_count += 1
            return run, False

    async def get_run(self, *, client_id: str, run_id: uuid.UUID) -> RunSnapshot:
        run = self._runs.get(run_id)
        if run is None or client_id != "test-client":
            raise RunNotFoundError(f"Run {run_id} was not found")
        return run

    async def get_attempts(self, *, client_id: str, run_id: uuid.UUID) -> list[AttemptSnapshot]:
        await self.get_run(client_id=client_id, run_id=run_id)
        return []

    async def get_events(self, *, client_id: str, run_id: uuid.UUID) -> list[EventSnapshot]:
        await self.get_run(client_id=client_id, run_id=run_id)
        return []


def _client(service: InMemoryRunService) -> TestClient:
    return TestClient(create_app(Settings(), run_service=service))


def test_identical_duplicate_returns_original_run() -> None:
    service = InMemoryRunService()
    with _client(service) as client:
        first = client.post("/v1/runs", headers=HEADERS, json={"input": {"prompt": "hello"}})
        duplicate = client.post("/v1/runs", headers=HEADERS, json={"input": {"prompt": "hello"}})

    assert first.status_code == 202
    assert duplicate.status_code == 202
    assert first.json()["run_id"] == duplicate.json()["run_id"]
    assert first.json()["replayed"] is False
    assert duplicate.json()["replayed"] is True
    assert service.submission_count == 1


def test_concurrent_identical_submissions_create_one_run() -> None:
    service = InMemoryRunService()

    async def submit_once() -> tuple[RunSnapshot, bool]:
        return await service.submit(
            client_id="test-client",
            idempotency_key="concurrent-0001",
            input_payload={"prompt": "hello"},
            policy_snapshot={},
        )

    async def submit_all() -> list[tuple[RunSnapshot, bool]]:
        return await asyncio.gather(*(submit_once() for _ in range(100)))

    results = asyncio.run(submit_all())

    assert len({run.id for run, _ in results}) == 1
    assert sum(replayed for _, replayed in results) == 99
    assert service.submission_count == 1


def test_idempotency_key_with_different_body_returns_conflict() -> None:
    service = InMemoryRunService()
    with _client(service) as client:
        client.post("/v1/runs", headers=HEADERS, json={"input": {"prompt": "first"}})
        response = client.post("/v1/runs", headers=HEADERS, json={"input": {"prompt": "second"}})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_missing_idempotency_key_is_a_standard_api_error() -> None:
    service = InMemoryRunService()
    with _client(service) as client:
        response = client.post(
            "/v1/runs", headers={"X-Client-Id": "test-client"}, json={"input": {"prompt": "hello"}}
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MISSING_IDEMPOTENCY_KEY"


def test_malformed_request_is_a_standard_api_error() -> None:
    service = InMemoryRunService()
    with _client(service) as client:
        response = client.post("/v1/runs", headers=HEADERS, json={"input": []})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_invalid_retry_policy_is_rejected_before_submission() -> None:
    service = InMemoryRunService()
    with _client(service) as client:
        response = client.post(
            "/v1/runs",
            headers=HEADERS,
            json={
                "input": {"prompt": "hello"},
                "policy": {"initial_backoff_seconds": 4, "max_backoff_seconds": 2},
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_RETRY_POLICY"
    assert service.submission_count == 0


def test_get_endpoints_return_empty_history_for_queued_run() -> None:
    service = InMemoryRunService()
    with _client(service) as client:
        created = client.post("/v1/runs", headers=HEADERS, json={"input": {"prompt": "hello"}})
        run_id = created.json()["run_id"]
        run = client.get(f"/v1/runs/{run_id}", headers={"X-Client-Id": "test-client"})
        attempts = client.get(f"/v1/runs/{run_id}/attempts", headers={"X-Client-Id": "test-client"})
        events = client.get(f"/v1/runs/{run_id}/events", headers={"X-Client-Id": "test-client"})

    assert run.status_code == 200
    assert run.json()["execution_status"] == "QUEUED"
    assert attempts.json() == []
    assert events.json() == []


def test_missing_run_returns_not_found() -> None:
    service = InMemoryRunService()
    with _client(service) as client:
        response = client.get(f"/v1/runs/{uuid.uuid4()}", headers={"X-Client-Id": "test-client"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RUN_NOT_FOUND"


def test_canonical_hash_ignores_key_order() -> None:
    assert canonical_request_hash({"a": 1, "b": 2}, {"timeout": 10}) == canonical_request_hash(
        {"b": 2, "a": 1}, {"timeout": 10}
    )
