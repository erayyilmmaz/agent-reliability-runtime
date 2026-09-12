"""Real SQL/HTTP checks. Run only against explicitly selected disposable services."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from agent_runtime.api.main import create_app
from agent_runtime.application.execution import ClaimDecision, ExecutionResult
from agent_runtime.application.runs import QuotaExceededError
from agent_runtime.evaluation.cli import _provider_registry
from agent_runtime.execution.resource_executor import ResourceExecutor
from agent_runtime.infrastructure.database.execution_service import ExecutionPersistenceService
from agent_runtime.infrastructure.database.models import (
    Evaluation,
    OutboxEvent,
    ProviderQuota,
    Run,
    RunAttempt,
    RunEvent,
)
from agent_runtime.infrastructure.database.quotas import QuotaLimits, charge_provider_call
from agent_runtime.infrastructure.database.run_service import SqlAlchemyRunService
from agent_runtime.infrastructure.database.session import (
    create_database_engine,
    create_session_factory,
)
from agent_runtime.infrastructure.messaging.worker import RabbitMqWorker
from agent_runtime.providers.registry import ProviderRegistry
from agent_runtime.security.credentials import issue_credential, registry_entry
from agent_runtime.settings import Settings

DB = os.environ.get("ARR_TEST_DATABASE_URL")
REDIS = os.environ.get("ARR_TEST_REDIS_URL")
pytestmark = pytest.mark.skipif(
    not DB or not REDIS, reason="Requires disposable ARR_TEST_DATABASE_URL and ARR_TEST_REDIS_URL"
)


@pytest.fixture(scope="module", autouse=True)
def migrate():
    if not DB:
        pytest.skip("No disposable database configured")
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env={
            **os.environ,
            "APP_DATABASE_URL": DB,
            "APP_ENVIRONMENT": "local",
            "APP_AUTH_MODE": "disabled",
        },
        check=True,
        capture_output=True,
    )


def profile(**overrides):
    suffix = uuid4().hex
    pepper = "disposable-resource-test-pepper" * 2
    keys, records = [], []
    for principal, tenant in (("a", "a"), ("a2", "a"), ("b", "b")):
        key, record = issue_credential(
            principal_id=f"p-{principal}-{suffix}", tenant_id=f"t-{tenant}-{suffix}", pepper=pepper
        )
        keys.append(key)
        records.append(record)
    settings = Settings(
        environment="test",
        auth_mode="api_key",
        auth_pepper=pepper,
        auth_credentials=json.dumps([registry_entry(r) for r in records]),
        database_url=DB,
        redis_url=REDIS,
        rate_limit_requests=1000,
        **overrides,
    )
    return settings, keys, records


class Provider:
    name = "deterministic"
    calls = 0

    async def execute(self, *, input_payload, policy_snapshot):
        self.calls += 1
        return ExecutionResult(provider=self.name, result_payload={"accepted_input": input_payload})


class Delivery:
    acks = 0
    nacks = 0

    async def ack(self):
        self.acks += 1

    async def nack(self, *, requeue):
        self.nacks += 1


def regression_payload(cases=2):
    return {
        "dataset": {
            "dataset_id": "resource-test",
            "version": "1",
            "cases": [
                {
                    "case_id": str(i),
                    "input": {"prompt": "ready"},
                    "rules": [{"type": "non_empty", "path": "accepted_input.prompt"}],
                }
                for i in range(cases)
            ],
        },
        "baseline": {"provider": "deterministic"},
        "candidate": {"provider": "deterministic"},
    }


async def test_durable_evaluation_regression_jobs_and_provider_budget():
    settings, keys, records = profile(principal_provider_calls=5)
    engine = create_database_engine(settings)
    sessions = create_session_factory(engine)
    provider = Provider()
    execution = ExecutionPersistenceService(sessions, lease_seconds=120)
    worker = RabbitMqWorker(
        url=str(settings.rabbitmq_url),
        worker_id="resource-test",
        prefetch_count=4,
        execution_service=execution,
        executor=ResourceExecutor(ProviderRegistry([provider]), sessions, settings),
    )

    async def process(job_id):
        delivery = Delivery()
        await worker._handle_run_delivery(delivery, UUID(job_id))
        assert (delivery.acks, delivery.nacks) == (1, 0)

    try:
        with TestClient(create_app(settings)) as client:
            headers = {"X-API-Key": keys[0], "Idempotency-Key": "source-run-0001"}
            response = client.post("/v1/runs", headers=headers, json={"input": {"prompt": "ready"}})
            assert response.status_code == 202
            source = response.json()["run_id"]
            assert provider.calls == 0
            await process(source)
            assert provider.calls == 1
            evaluation = client.post(
                f"/v1/runs/{source}/evaluations",
                headers=headers,
                json={"rules": [{"type": "non_empty", "path": "accepted_input.prompt"}]},
            )
            assert evaluation.status_code == 202 and evaluation.json()["status"] == "PENDING"
            job_id = evaluation.json()["details"]["job_run_id"]
            async with sessions() as session:
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(OutboxEvent)
                        .where(OutboxEvent.aggregate_id == UUID(job_id))
                    )
                    == 1
                )
            assert (
                client.get(f"/v1/jobs/{job_id}", headers=headers).json()["execution_status"]
                == "QUEUED"
            )
            assert client.get("/healthz").status_code == 200
            await process(job_id)
            assert (
                client.get(f"/v1/runs/{source}/evaluations", headers=headers).json()[0]["status"]
                == "PASSED"
            )
            await process(job_id)
            assert provider.calls == 1
            job_headers = {**headers, "Idempotency-Key": "regression-job-0001"}
            response = client.post(
                "/v1/evaluation-regressions", headers=job_headers, json=regression_payload()
            )
            assert response.status_code == 202
            regression_id = response.json()["job_id"]
            assert provider.calls == 1
            assert (
                client.get(
                    f"/v1/jobs/{regression_id}",
                    headers={"X-API-Key": keys[2], "X-Client-Id": records[0].tenant_id},
                ).status_code
                == 404
            )
            await process(regression_id)
            report = client.get(f"/v1/jobs/{regression_id}", headers=headers).json()
            assert report["execution_status"] == "SUCCEEDED" and report["result"]["passed"]
            assert provider.calls == 5
            await process(regression_id)
            assert provider.calls == 5
            duplicate = client.post(
                "/v1/evaluation-regressions", headers=job_headers, json=regression_payload()
            )
            assert duplicate.json()["job_id"] == regression_id and duplicate.json()["replayed"]
            blocked = client.post(
                "/v1/evaluation-regressions",
                headers={**headers, "Idempotency-Key": "regression-job-0002"},
                json=regression_payload(),
            )
            await process(blocked.json()["job_id"])
            assert (
                client.get(blocked.json()["status_url"], headers=headers).json()["error_code"]
                == "RESOURCE_QUOTA_EXCEEDED"
            )
            assert provider.calls == 5
            assert (
                client.post(
                    "/v1/evaluation-regressions", headers=headers, json=regression_payload(11)
                ).status_code
                == 422
            )
            assert (
                client.post("/v1/evaluation-regressions", json=regression_payload()).status_code
                == 401
            )
    finally:
        await engine.dispose()


async def test_concurrent_admission_and_tenant_caps():
    settings, keys, records = profile(principal_concurrent_runs=2, tenant_concurrent_runs=3)
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):

        async def submit(key, index):
            return await client.post(
                "/v1/runs",
                headers={"X-API-Key": key, "Idempotency-Key": f"quota-run-{index:04d}"},
                json={"input": {"prompt": "quota"}},
            )

        responses = await asyncio.gather(*(submit(keys[0], i) for i in range(10)))
        assert sum(r.status_code == 202 for r in responses) == 2
        assert sum(r.status_code == 429 for r in responses) == 8
        accepted = next(i for i, r in enumerate(responses) if r.status_code == 202)
        assert (await submit(keys[0], accepted)).json()["replayed"]
        assert (await submit(keys[1], 11)).status_code == 202
        assert (await submit(keys[1], 12)).status_code == 429
        assert (await submit(keys[2], 13)).status_code == 202


async def test_running_capacity_defers_without_losing_work_and_recovers():
    settings, _, records = profile()
    engine = create_database_engine(settings)
    sessions = create_session_factory(engine)
    service = SqlAlchemyRunService(sessions)
    execution = ExecutionPersistenceService(sessions, lease_seconds=120)
    try:
        runs = []
        for i, identity in enumerate((records[0], records[0], records[2])):
            run, _ = await service.submit(
                client_id=identity.tenant_id,
                principal_id=identity.principal_id,
                idempotency_key=f"capacity-run-{i}",
                input_payload={"prompt": "ready"},
                policy_snapshot=service.job_policy(),
            )
            runs.append(run)
        claims = await asyncio.gather(
            *(execution.claim(run_id=r.id, worker_id=f"worker-{i}") for i, r in enumerate(runs))
        )
        assert sum(c.decision == ClaimDecision.CLAIMED for c in claims[:2]) == 1
        assert claims[2].decision == ClaimDecision.CLAIMED
        pending_index = next(i for i in range(2) if claims[i].decision == ClaimDecision.NOT_READY)
        active_index = 1 - pending_index
        async with sessions() as session:
            pending = await session.get(Run, runs[pending_index].id)
            assert pending.execution_status == "RETRY_SCHEDULED"
            assert pending.next_attempt_at > datetime.now(UTC)
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(RunAttempt)
                    .where(RunAttempt.run_id == pending.id)
                )
                == 0
            )
        active = claims[active_index]
        assert await execution.complete_success(
            run_id=active.run_id,
            attempt_id=active.attempt_id,
            worker_id=f"worker-{active_index}",
            result=ExecutionResult("deterministic", {"ready": True}),
        )
        async with sessions.begin() as session:
            pending = await session.get(Run, runs[pending_index].id)
            pending.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        await execution.schedule_due_retries()
        assert (
            await execution.claim(run_id=runs[pending_index].id, worker_id="resumed")
        ).decision == ClaimDecision.CLAIMED
    finally:
        await engine.dispose()


async def test_atomic_provider_quota_races():
    settings, _, records = profile()
    engine = create_database_engine(settings)
    sessions = create_session_factory(engine)
    limits = QuotaLimits(principal_calls=7, tenant_calls=7)
    identity = records[0]

    async def charge(principal, tenant):
        try:
            async with sessions.begin() as session:
                await charge_provider_call(session, principal, tenant, limits)
            return True
        except QuotaExceededError:
            return False

    try:
        results = await asyncio.gather(
            *(charge(identity.principal_id, identity.tenant_id) for _ in range(30))
        )
        assert sum(results) == 7
        assert not await charge(records[1].principal_id, identity.tenant_id)
        assert await charge(records[2].principal_id, records[2].tenant_id)
        async with sessions() as session:
            assert (await session.get(ProviderQuota, f"p:{identity.principal_id}")).used == 7
            assert (await session.get(ProviderQuota, f"t:{identity.tenant_id}")).used == 7
    finally:
        await engine.dispose()


async def test_real_http_timeout_changes_with_configuration():
    async def handler(reader, writer):
        try:
            await reader.read(16384)
            await asyncio.sleep(1.2)
            body = b'{"output_text":"ready"}'
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        for timeout in (1, 2):
            settings = Settings(
                environment="local",
                auth_mode="disabled",
                openai_api_key="test-only",
                openai_base_url=f"http://127.0.0.1:{port}",
                provider_timeout_seconds=timeout,
            )
            request = _provider_registry(settings).execute(
                provider="openai", input_payload={"prompt": "timeout-test"}, policy_snapshot={}
            )
            if timeout == 1:
                with pytest.raises(TimeoutError):
                    await request
            else:
                assert (await request).result_payload["output_text"] == "ready"


async def test_history_cursor_pages_are_bounded_stable_and_tenant_scoped():
    settings, keys, records = profile()
    engine = create_database_engine(settings)
    sessions = create_session_factory(engine)
    service = SqlAlchemyRunService(sessions)
    try:
        run, _ = await service.submit(
            client_id=records[0].tenant_id,
            principal_id=records[0].principal_id,
            idempotency_key="pagination-source-01",
            input_payload={},
            policy_snapshot={},
        )
        now = datetime.now(UTC)
        async with sessions.begin() as session:
            for i in range(105):
                # Identical timestamps exercise the UUID tie-breaker, not just offset paging.
                session.add(
                    RunAttempt(
                        run_id=run.id,
                        attempt_number=i + 1,
                        provider="deterministic",
                        worker_id="fixture",
                        started_at=now,
                    )
                )
                session.add(RunEvent(run_id=run.id, event_type="PAGE_FIXTURE", created_at=now))
                session.add(
                    Evaluation(run_id=run.id, evaluator="fixture", status="PASSED", created_at=now)
                )
        with TestClient(create_app(settings)) as client:
            headers = {"X-API-Key": keys[0]}
            for resource, count, id_field in (
                ("attempts", 105, "attempt_id"),
                ("events", 106, "event_id"),
                ("evaluations", 105, "evaluation_id"),
            ):
                path = f"/v1/runs/{run.id}/{resource}"
                first = client.get(path, headers=headers)
                assert first.status_code == 200 and len(first.json()) == 50
                seen = [row[id_field] for row in first.json()]
                cursor = first.headers["X-Next-Cursor"]
                while cursor:
                    page = client.get(path, headers=headers, params={"cursor": cursor, "limit": 20})
                    assert page.status_code == 200 and len(page.json()) <= 20
                    seen.extend(row[id_field] for row in page.json())
                    cursor = page.headers.get("X-Next-Cursor")
                assert len(seen) == len(set(seen)) == count
                assert client.get(path, headers=headers, params={"limit": 101}).status_code == 422
                assert (
                    client.get(path, headers=headers, params={"cursor": str(uuid4())}).status_code
                    == 422
                )
                assert (
                    client.get(
                        path, headers={"X-API-Key": keys[2]}, params={"cursor": seen[0]}
                    ).status_code
                    == 404
                )
    finally:
        await engine.dispose()


async def test_expired_evaluation_job_finishes_error_and_releases_capacity():
    settings, _, records = profile()
    engine = create_database_engine(settings)
    sessions = create_session_factory(engine)
    identity = records[0]
    service = SqlAlchemyRunService(sessions)
    execution = ExecutionPersistenceService(sessions, lease_seconds=120)
    try:
        source, _ = await service.submit(
            client_id=identity.tenant_id,
            principal_id=identity.principal_id,
            idempotency_key="recovery-source-01",
            input_payload={},
            policy_snapshot={},
        )
        claim = await execution.claim(run_id=source.id, worker_id="source")
        await execution.complete_success(
            run_id=source.id,
            attempt_id=claim.attempt_id,
            worker_id="source",
            result=ExecutionResult("deterministic", {"ok": True}),
        )
        evaluation = await service.evaluate(
            client_id=identity.tenant_id,
            run_id=source.id,
            principal_id=identity.principal_id,
            rules=[{"type": "non_empty"}],
        )
        job_id = UUID(evaluation.details["job_run_id"])
        job = await execution.claim(run_id=job_id, worker_id="crashed")
        async with sessions.begin() as session:
            persisted = await session.get(Run, job_id)
            persisted.execution_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        assert await execution.recover_expired_leases() >= 1
        async with sessions() as session:
            persisted = await session.get(Run, job_id)
            assert persisted.execution_status == "DEAD_LETTERED"
            assert persisted.execution_lease_owner is None
            assert (await session.get(Evaluation, evaluation.id)).status == "ERROR"
            original = await session.get(Run, source.id)
            assert (
                original.execution_status == "SUCCEEDED" and original.evaluation_status == "ERROR"
            )
        assert not await execution.complete_success(
            run_id=job_id,
            attempt_id=job.attempt_id,
            worker_id="crashed",
            result=ExecutionResult("evaluation", {"passed": True}),
        )
        replacement = await service.evaluate(
            client_id=identity.tenant_id,
            run_id=source.id,
            principal_id=identity.principal_id,
            rules=[{"type": "non_empty"}],
        )
        assert (
            await execution.claim(
                run_id=UUID(replacement.details["job_run_id"]), worker_id="replacement"
            )
        ).decision == ClaimDecision.CLAIMED
    finally:
        await engine.dispose()


def test_regression_server_budget_rejects_before_job_admission():
    settings, keys, _ = profile(regression_provider_call_budget=2)
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/evaluation-regressions",
            json=regression_payload(2),
            headers={"X-API-Key": keys[0], "Idempotency-Key": "too-many-calls-01"},
        )
        assert response.status_code == 422
