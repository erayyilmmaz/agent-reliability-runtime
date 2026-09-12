from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_run_submission_api import InMemoryRunService

from agent_runtime.api.main import create_app
from agent_runtime.application.runs import RunSnapshot
from agent_runtime.domain.states import EvaluationStatus, ExecutionStatus
from agent_runtime.infrastructure.redis.rate_limiter import (
    NoopRateLimiter,
    RateLimitDecision,
    RateLimitUnavailable,
)
from agent_runtime.security.audit import SecurityAuditRecord
from agent_runtime.security.credentials import issue_credential, registry_entry
from agent_runtime.settings import Settings

PEPPER = "integration-only-pepper-" * 2
KEY, RECORD = issue_credential(principal_id="alice", tenant_id="tenant-a", pepper=PEPPER)
KEY_B, RECORD_B = issue_credential(principal_id="bob", tenant_id="tenant-b", pepper=PEPPER)
KEY_ROTATED, RECORD_ROTATED = issue_credential(
    principal_id="alice", tenant_id="tenant-a", pepper=PEPPER
)


def secured_settings(**kwargs: Any) -> Settings:
    return Settings(
        auth_mode="api_key",
        auth_pepper=PEPPER,
        auth_credentials=json.dumps(
            [registry_entry(r) for r in (RECORD, RECORD_B, RECORD_ROTATED)]
        ),
        **kwargs,
    )


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
        principal_id: str | None = None,
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


@pytest.mark.parametrize("policy,expected", [("fail_closed", 503), ("fail_open", 202)])
def test_audit_failure_policy_is_explicit_and_metered(policy, expected, monkeypatch):
    from agent_runtime.observability.metrics import get_runtime_metrics

    events = []
    monkeypatch.setattr(get_runtime_metrics(), "security_event", lambda *a: events.append(a))

    class FailingSink:
        async def record(self, record):
            raise RuntimeError("private db error body")

    service = CountingRunService()
    with TestClient(
        create_app(
            secured_settings(audit_failure_policy=policy),
            run_service=service,
            rate_limiter=NoopRateLimiter(),
            audit_sink=FailingSink(),
        )
    ) as client:
        response = client.post("/v1/runs", headers=_headers(), json={"input": {"prompt": "secret"}})
        assert response.status_code == expected
        assert "private" not in response.text
    assert service.submission_count == (1 if policy == "fail_open" else 0)
    assert ("audit_write", "error") in events


def test_sensitive_read_audits_success_denial_and_fail_closed_delivery():
    service = InMemoryRunService()
    audit = RecordingAuditSink()
    with TestClient(
        create_app(
            secured_settings(),
            run_service=service,
            rate_limiter=NoopRateLimiter(),
            audit_sink=audit,
        )
    ) as client:
        created = client.post("/v1/runs", headers=_headers(), json={"input": {"prompt": "secret"}})
        run_id = created.json()["run_id"]
        for suffix, resource in [
            ("", "run"),
            ("/attempts", "attempts"),
            ("/events", "events"),
            ("/evaluations", "evaluations"),
        ]:
            assert client.get(f"/v1/runs/{run_id}{suffix}", headers=_headers()).status_code == 200
            record = audit.records[-1]
            assert record.event_type == "SENSITIVE_READ" and record.outcome == "ALLOWED"
            assert str(record.target_run_id) == run_id and record.principal_id == "alice"
            assert record.resource == resource
        assert client.get(f"/v1/runs/{run_id}", headers={"X-API-Key": KEY_B}).status_code == 404
        assert audit.records[-1].outcome == "DENIED" and audit.records[-1].principal_id == "bob"

    class FailingReadSink(RecordingAuditSink):
        async def record(self, record):
            if record.event_type == "SENSITIVE_READ":
                raise RuntimeError("audit unavailable")
            await super().record(record)

    with TestClient(
        create_app(
            secured_settings(),
            run_service=service,
            rate_limiter=NoopRateLimiter(),
            audit_sink=FailingReadSink(),
        )
    ) as client:
        denied = client.get(f"/v1/runs/{run_id}", headers=_headers())
        assert denied.status_code == 503 and "execution_status" not in denied.text


class SequenceRateLimiter:
    def __init__(self, decisions: list[RateLimitDecision]) -> None:
        self._decisions = decisions
        self.client_ids: list[str] = []

    async def check(self, client_id: str) -> RateLimitDecision:
        self.client_ids.append(client_id)
        return self._decisions.pop(0)

    async def close(self) -> None:
        return None


@pytest.mark.parametrize(
    "trust,duplicate,expected", [(False, False, False), (True, False, True), (True, True, False)]
)
def test_inbound_trace_trust_policy_controls_persisted_context(
    trust, duplicate, expected, monkeypatch
):
    from opentelemetry.sdk.trace import TracerProvider

    from agent_runtime.observability import telemetry

    provider = TracerProvider()
    monkeypatch.setattr(telemetry, "get_tracer", lambda: provider.get_tracer("test"))
    parent = "00-00000000000000000000000000000001-0000000000000002-01"
    contexts = []

    class TraceService(CountingRunService):
        async def submit(self, **kwargs):
            contexts.append(telemetry.inject_trace_context())
            return await super().submit(**kwargs)

    headers = list(_headers().items()) + [("traceparent", parent)]
    if duplicate:
        headers.append(("traceparent", parent))
    with TestClient(
        create_app(
            secured_settings(trust_inbound_trace_context=trust),
            run_service=TraceService(),
            rate_limiter=NoopRateLimiter(),
        )
    ) as client:
        assert (
            client.post(
                "/v1/runs", headers=headers, json={"input": {"prompt": "ready"}}
            ).status_code
            == 202
        )
    assert (contexts[0]["traceparent"][3:35] == parent[3:35]) is expected
    provider.shutdown()


def test_audit_write_deadline_prevents_hanging_request():
    import asyncio

    class SlowSink:
        async def record(self, record):
            await asyncio.sleep(10)

    with TestClient(
        create_app(
            secured_settings(audit_write_timeout_seconds=0.01),
            run_service=CountingRunService(),
            rate_limiter=NoopRateLimiter(),
            audit_sink=SlowSink(),
        )
    ) as client:
        assert (
            client.post(
                "/v1/runs", headers=_headers(), json={"input": {"prompt": "ready"}}
            ).status_code
            == 503
        )


def _headers(**extra: str) -> dict[str, str]:
    return {
        "X-API-Key": KEY,
        "X-Client-Id": "security-test-client",
        "Idempotency-Key": "security-test-run-0001",
        **extra,
    }


def _client(
    service: CountingRunService, limiter: SequenceRateLimiter, audit: RecordingAuditSink
) -> TestClient:
    return TestClient(
        create_app(
            secured_settings(max_request_bytes=1024),
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
    assert audit.records[-1].credential_fingerprint is None
    assert audit.records[-1].client_id is None
    assert "wrong-secret" not in json.dumps([record.__dict__ for record in audit.records])


def test_shared_limiter_blocks_before_run_submission() -> None:
    service = CountingRunService()
    limiter = SequenceRateLimiter([RateLimitDecision(True, 3), RateLimitDecision(False, 3)])
    audit = RecordingAuditSink()
    with _client(service, limiter, audit) as client:
        first = client.post("/v1/runs", headers=_headers(), json={"input": {"prompt": "x"}})
        second = client.post(
            "/v1/runs",
            headers=_headers(
                **{
                    "Idempotency-Key": "security-test-run-0002",
                    "X-Client-Id": "rotated-header",
                    "X-API-Key": KEY_ROTATED,
                }
            ),
            json={"input": {"prompt": "x"}},
        )

    assert first.status_code == 202
    assert second.status_code == 429
    assert second.headers["retry-after"] == "3"
    assert service.submission_count == 1
    assert audit.records[-1].reason == "RATE_LIMIT_EXCEEDED"
    assert limiter.client_ids == ["alice", "alice"]
    assert audit.records[-1].client_id == "tenant-a"
    serialized = json.dumps([record.__dict__ for record in audit.records])
    assert KEY not in serialized
    assert RECORD.verifier.get_secret_value() not in serialized


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


@pytest.mark.parametrize(
    "value", ["x" * 129, "x" * 10000, "bad value", "", " leading", "trailing ", "a/b"]
)
def test_invalid_identity_returns_422_without_audit_or_persistence(value: str) -> None:
    service, limiter, audit = CountingRunService(), SequenceRateLimiter([]), RecordingAuditSink()
    with _client(service, limiter, audit) as client:
        response = client.post(
            "/v1/runs", headers=_headers(**{"X-Client-Id": value}), json={"input": {"prompt": "x"}}
        )
    assert response.status_code == 422
    assert not audit.records
    assert not limiter.client_ids
    assert service.submission_count == 0


def test_maximum_length_identity_is_valid_but_cannot_set_tenant() -> None:
    service = InMemoryRunService()
    with TestClient(
        create_app(secured_settings(), run_service=service, rate_limiter=NoopRateLimiter())
    ) as client:
        response = client.post(
            "/v1/runs",
            headers=_headers(**{"X-Client-Id": "a" * 128}),
            json={"input": {"prompt": "x"}},
        )
    assert response.status_code == 202
    assert set(service._owners.values()) == {"tenant-a"}


def test_duplicate_client_identity_is_rejected() -> None:
    service, limiter, audit = CountingRunService(), SequenceRateLimiter([]), RecordingAuditSink()
    with _client(service, limiter, audit) as client:
        response = client.get(
            "/v1/runs/" + str(uuid.uuid4()),
            headers=[("X-API-Key", KEY), ("X-Client-Id", "first"), ("X-Client-Id", "second")],
        )
    assert response.status_code == 422
    assert not audit.records
    assert not limiter.client_ids


def test_tenant_isolation_for_all_run_evaluation_and_replay_routes() -> None:
    service = InMemoryRunService()
    with TestClient(
        create_app(secured_settings(), run_service=service, rate_limiter=NoopRateLimiter())
    ) as client:
        # No X-Client-Id is required in authenticated mode.
        owner = {"X-API-Key": KEY, "Idempotency-Key": "isolation-run-0001"}
        created = client.post("/v1/runs", headers=owner, json={"input": {"prompt": "private"}})
        assert created.status_code == 202
        run_id = created.json()["run_id"]
        attacker = {
            "X-API-Key": KEY_B,
            "X-Client-Id": "tenant-a",
            "Idempotency-Key": "isolation-replay-0001",
        }
        for suffix in ("", "/attempts", "/events", "/evaluations"):
            assert client.get(f"/v1/runs/{run_id}{suffix}", headers=attacker).status_code == 404
            assert client.get(f"/v1/runs/{run_id}{suffix}", headers=owner).status_code == 200
        assert (
            client.post(
                f"/v1/runs/{run_id}/evaluations",
                headers=attacker,
                json={"rules": [{"type": "non_empty"}]},
            ).status_code
            == 404
        )
        assert client.post(f"/v1/runs/{run_id}/replay", headers=attacker).status_code == 404
        assert (
            client.post(
                f"/v1/runs/{run_id}/replay",
                headers={**owner, "Idempotency-Key": "owner-replay-0001"},
            ).status_code
            == 202
        )
        other = client.post(
            "/v1/runs", headers={**owner, "X-API-Key": KEY_B}, json={"input": {"prompt": "private"}}
        )
        assert other.status_code == 202
        assert other.json()["run_id"] != run_id
        duplicate = client.post(
            "/v1/runs",
            headers={**owner, "X-API-Key": KEY_ROTATED, "X-Client-Id": "tenant-b"},
            json={"input": {"prompt": "private"}},
        )
        assert duplicate.json()["run_id"] == run_id
        assert duplicate.json()["replayed"]


def test_public_allowlist_and_docs_cover_routes_outside_v1() -> None:
    app = create_app(
        secured_settings(), run_service=CountingRunService(), rate_limiter=NoopRateLimiter()
    )

    @app.get("/future-private-route")
    async def future_route():
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        for path in (
            "/healthz/private",
            "/healthz/",
            "/future-private-route",
            "/docs",
            "/redoc",
            "/openapi.json",
        ):
            assert client.get(path).status_code == 401
        assert client.post("/healthz").status_code == 401
        assert client.get("/future-private-route", headers={"X-API-Key": KEY}).status_code == 200
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path, headers={"X-API-Key": KEY}).status_code == 404


def test_headerless_regression_route_is_rate_limited_and_redis_failure_is_closed() -> None:
    service, audit = CountingRunService(), RecordingAuditSink()
    limiter = SequenceRateLimiter([RateLimitDecision(False, 2)])
    with _client(service, limiter, audit) as client:
        response = client.post("/v1/evaluation-regressions", headers={"X-API-Key": KEY}, json={})
    assert response.status_code == 429
    assert limiter.client_ids == ["alice"]

    class UnavailableLimiter:
        async def check(self, client_id: str):
            raise RateLimitUnavailable("offline")

    with TestClient(
        create_app(
            secured_settings(),
            run_service=service,
            rate_limiter=UnavailableLimiter(),
            audit_sink=audit,
        )
    ) as client:
        assert (
            client.post("/v1/runs", headers=_headers(), json={"input": {"prompt": "x"}}).status_code
            == 503
        )
    assert service.submission_count == 0
