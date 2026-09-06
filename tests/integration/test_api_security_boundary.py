from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient

from agent_runtime.api.main import create_app
from agent_runtime.application.runs import RunSnapshot
from agent_runtime.domain.states import EvaluationStatus, ExecutionStatus
from agent_runtime.infrastructure.redis.rate_limiter import RateLimitDecision
from agent_runtime.security.audit import SecurityAuditRecord
from agent_runtime.settings import Settings


class CountingRunService:
    def __init__(self) -> None:
        self.submission_count = 0

    async def submit(
        self,
        *,
        client_id: str,
        idempotency_key: str,
        input_payload: dict[str, Any],
        policy_snapshot: dict[str, Any],
    ) -> tuple[RunSnapshot, bool]:
        del client_id, idempotency_key, input_payload, policy_snapshot
        self.submission_count += 1
        return (
            RunSnapshot(
                id=uuid.uuid4(),
                execution_status=ExecutionStatus.QUEUED,
                evaluation_status=EvaluationStatus.NOT_RUN,
                created_at=datetime.now(UTC),
                started_at=None,
                completed_at=None,
                replay_of_run_id=None,
                error_code=None,
            ),
            False,
        )


class RecordingAuditSink:
    def __init__(self) -> None:
        self.records: list[SecurityAuditRecord] = []

    async def record(self, record: SecurityAuditRecord) -> None:
        self.records.append(record)


class SequenceRateLimiter:
    def __init__(self, decisions: list[RateLimitDecision]) -> None:
        self._decisions = decisions
        self.client_ids: list[str] = []

    async def check(self, client_id: str) -> RateLimitDecision:
        self.client_ids.append(client_id)
        return self._decisions.pop(0)

    async def close(self) -> None:
        return None


def _key_hash() -> str:
    return hashlib.sha256(b"integration-test-api-key").hexdigest()


def _headers(**extra: str) -> dict[str, str]:
    return {
        "X-API-Key": "integration-test-api-key",
        "X-Client-Id": "security-test-client",
        "Idempotency-Key": "security-test-run-0001",
        **extra,
    }


def _client(
    service: CountingRunService, limiter: SequenceRateLimiter, audit: RecordingAuditSink
) -> TestClient:
    return TestClient(
        create_app(
            Settings(auth_mode="api_key", auth_api_key_hash=_key_hash(), max_request_bytes=1024),
            run_service=service,
            rate_limiter=limiter,
            audit_sink=audit,
        )
    )


def test_missing_and_invalid_credentials_do_not_submit_a_run_or_leak_raw_key() -> None:
    service = CountingRunService()
    limiter = SequenceRateLimiter([RateLimitDecision(True, 1)])
    audit = RecordingAuditSink()
    with _client(service, limiter, audit) as client:
        missing = client.post(
            "/v1/runs",
            headers={"X-Client-Id": "security-test-client"},
            json={"input": {"prompt": "x"}},
        )
        invalid = client.post(
            "/v1/runs",
            headers={**_headers(**{"X-API-Key": "wrong-secret"})},
            json={"input": {"prompt": "x"}},
        )

    assert missing.status_code == 401
    assert invalid.status_code == 403
    assert service.submission_count == 0
    assert audit.records[-1].credential_fingerprint == hashlib.sha256(b"wrong-secret").hexdigest()
    assert "wrong-secret" not in json.dumps([record.__dict__ for record in audit.records])


def test_shared_limiter_blocks_before_run_submission() -> None:
    service = CountingRunService()
    limiter = SequenceRateLimiter([RateLimitDecision(True, 3), RateLimitDecision(False, 3)])
    audit = RecordingAuditSink()
    with _client(service, limiter, audit) as client:
        first = client.post("/v1/runs", headers=_headers(), json={"input": {"prompt": "x"}})
        second = client.post(
            "/v1/runs",
            headers=_headers(**{"Idempotency-Key": "security-test-run-0002"}),
            json={"input": {"prompt": "x"}},
        )

    assert first.status_code == 202
    assert second.status_code == 429
    assert second.headers["retry-after"] == "3"
    assert service.submission_count == 1
    assert audit.records[-1].reason == "RATE_LIMIT_EXCEEDED"


def test_oversized_request_is_rejected_before_auth_or_provider_work() -> None:
    service = CountingRunService()
    limiter = SequenceRateLimiter([])
    audit = RecordingAuditSink()
    with _client(service, limiter, audit) as client:
        response = client.post(
            "/v1/runs",
            headers=_headers(**{"Content-Length": "2048"}),
            content=b"{}",
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "REQUEST_TOO_LARGE"
    assert service.submission_count == 0
    assert limiter.client_ids == []
