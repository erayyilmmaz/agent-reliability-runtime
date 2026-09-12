"""PERF-E5: machine-independent performance regressions.

Absolute latency thresholds do not transfer between machines - this repository's
baseline was measured on a 10-core workstation, while CI runs on ~4 shared
vCPUs - so gating pull requests on milliseconds produces noise, not signal.

What does transfer is *structural*: how many rows a request writes, how many
durable records a rejection creates, and whether query count grows with result
size. Those are deterministic, and they are exactly the properties the
remediations in docs/performance-audit.md established. A regression in any of
them would return the measured gains, and would do so silently.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from agent_runtime.api.main import create_app
from agent_runtime.infrastructure.database.models import (
    OutboxEvent,
    Run,
    RunAttempt,
    RunEvent,
    SecurityAuditEvent,
)
from agent_runtime.infrastructure.database.session import (
    create_database_engine,
    create_session_factory,
)
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


def profile():
    pepper = "disposable-performance-invariants-pepper" * 2
    key, record = issue_credential(
        principal_id=f"perf-{uuid4().hex[:8]}", tenant_id=f"tenant-{uuid4().hex[:8]}", pepper=pepper
    )
    settings = Settings(
        environment="test",
        auth_mode="api_key",
        auth_pepper=pepper,
        auth_credentials=json.dumps([registry_entry(record)]),
        database_url=DB,
        redis_url=REDIS,
        rate_limit_requests=10000,
    )
    return settings, key, record


class StatementCounter:
    """Counts SQL statements a block of work issues, via SQLAlchemy events."""

    def __init__(self, engine):
        self._sync = engine.sync_engine
        self.statements: list[str] = []

    def __enter__(self):
        event.listen(self._sync, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc):
        event.remove(self._sync, "before_cursor_execute", self._record)

    def _record(self, conn, cursor, statement, parameters, context, executemany):
        self.statements.append(statement)

    def __len__(self) -> int:
        return len(self.statements)


async def test_read_writes_exactly_one_audit_row(anyio_backend=None):
    """PERF-003: reads are the highest-volume path; two rows per read was the
    request path's main write amplification."""
    settings, key, _ = profile()
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)

    try:
        with TestClient(create_app(settings)) as client:
            created = client.post(
                "/v1/runs",
                headers={"X-API-Key": key, "Idempotency-Key": f"inv-{uuid4().hex}"},
                json={"input": {"prompt": "invariant"}},
            )
            assert created.status_code == 202
            run_id = created.json()["run_id"]

            async with factory() as session:
                before = int(
                    await session.scalar(select(func.count()).select_from(SecurityAuditEvent)) or 0
                )

            reads = 20
            for _ in range(reads):
                assert (
                    client.get(f"/v1/runs/{run_id}", headers={"X-API-Key": key}).status_code == 200
                )

            async with factory() as session:
                after = int(
                    await session.scalar(select(func.count()).select_from(SecurityAuditEvent)) or 0
                )

        assert after - before == reads, (
            f"expected exactly {reads} audit rows for {reads} reads, got {after - before}. "
            "Two rows per read was PERF-003."
        )
    finally:
        await engine.dispose()


async def test_history_query_count_does_not_grow_with_result_size(anyio_backend=None):
    """No N+1: a page of 50 rows must cost the same number of queries as a page of 1.

    The audit found no N+1 anywhere. This asserts it stays that way, which is
    the regression that would scale worst and show up latest.
    """
    settings, key, _ = profile()
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)

    try:
        with TestClient(create_app(settings)) as client:
            created = client.post(
                "/v1/runs",
                headers={"X-API-Key": key, "Idempotency-Key": f"n1-{uuid4().hex}"},
                json={"input": {"prompt": "n plus one"}},
            )
            run_id = created.json()["run_id"]

            # One event exists from submission; add many more directly.
            async with factory() as session:
                async with session.begin():
                    for index in range(60):
                        session.add(
                            RunEvent(
                                run_id=run_id,
                                event_type="PERF_INVARIANT_PROBE",
                                metadata_={"index": index},
                            )
                        )

            with StatementCounter(engine) as small:
                assert (
                    client.get(
                        f"/v1/runs/{run_id}/events?limit=1", headers={"X-API-Key": key}
                    ).status_code
                    == 200
                )
            with StatementCounter(engine) as large:
                large_response = client.get(
                    f"/v1/runs/{run_id}/events?limit=50", headers={"X-API-Key": key}
                )
                assert large_response.status_code == 200

            assert len(large_response.json()) > 10, "the page must actually be large"
            assert len(large) == len(small), (
                f"query count grew with result size: {len(small)} for 1 row, "
                f"{len(large)} for 50. That is an N+1."
            )
    finally:
        await engine.dispose()


async def test_rejected_submission_creates_no_durable_work(anyio_backend=None):
    """PERF-004: 93.5% of runs in the audit traversed the whole pipeline only to
    be rejected at the worker. A rejection must stay cheap."""
    settings, key, record = profile()
    settings = settings.model_copy(update={"principal_provider_calls": 1})
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)

    from agent_runtime.infrastructure.database.quotas import QuotaLimits, charge_provider_call

    limits = QuotaLimits.from_settings(settings)
    try:
        async with factory() as session:
            async with session.begin():
                await charge_provider_call(session, record.principal_id, record.tenant_id, limits)

        async def counts() -> tuple[int, ...]:
            async with factory() as session:
                totals = []
                for model in (Run, OutboxEvent, RunAttempt):
                    totals.append(
                        int(await session.scalar(select(func.count()).select_from(model)) or 0)
                    )
                return tuple(totals)

        before = await counts()
        with TestClient(create_app(settings)) as client:
            rejected = client.post(
                "/v1/runs",
                headers={"X-API-Key": key, "Idempotency-Key": f"rej-{uuid4().hex}"},
                json={"input": {"prompt": "over budget"}},
            )
        assert rejected.status_code == 429
        assert await counts() == before, "a rejected submission created durable work"
    finally:
        await engine.dispose()
