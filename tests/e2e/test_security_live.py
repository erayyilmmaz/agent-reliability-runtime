"""SEC-E1 checks against disposable PostgreSQL/Redis, enabled explicitly in CI."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime

import psycopg
import pytest
from fastapi.testclient import TestClient
from redis.asyncio import Redis

from agent_runtime.api.main import create_app
from agent_runtime.infrastructure.redis.rate_limiter import RedisFixedWindowRateLimiter
from agent_runtime.security.credentials import issue_credential, registry_entry
from agent_runtime.settings import Settings

DATABASE_URL = os.environ.get("ARR_TEST_DATABASE_URL")
REDIS_URL = os.environ.get("ARR_TEST_REDIS_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL or not REDIS_URL,
    reason="Requires explicitly configured disposable ARR_TEST_DATABASE_URL and ARR_TEST_REDIS_URL",
)


def test_real_database_tenant_isolation_and_safe_audit() -> None:
    assert DATABASE_URL and REDIS_URL
    # This only upgrades the explicitly selected test database; it never drops a schema.
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env={
            **os.environ,
            "APP_DATABASE_URL": DATABASE_URL,
            "APP_ENVIRONMENT": "local",
            "APP_AUTH_MODE": "disabled",
        },
        check=True,
        capture_output=True,
    )
    suffix = uuid.uuid4().hex
    tenant_a, tenant_b = f"tenant-a-{suffix}", f"tenant-b-{suffix}"
    pepper = "disposable-test-pepper-" * 3
    key_a, record_a = issue_credential(
        principal_id=f"alice-{suffix}", tenant_id=tenant_a, pepper=pepper
    )
    key_b, record_b = issue_credential(
        principal_id=f"bob-{suffix}", tenant_id=tenant_b, pepper=pepper
    )
    key_new, record_new = issue_credential(
        principal_id=record_a.principal_id, tenant_id=tenant_a, pepper=pepper
    )
    settings = Settings(
        auth_mode="api_key",
        environment="test",
        auth_pepper=pepper,
        auth_credentials=json.dumps([registry_entry(r) for r in (record_a, record_b, record_new)]),
        database_url=DATABASE_URL,
        redis_url=REDIS_URL,
    )
    dsn = DATABASE_URL.replace("postgresql+asyncpg", "postgresql")
    started = datetime.now(UTC)
    with TestClient(create_app(settings)) as client, psycopg.connect(dsn) as conn:
        owner = {"X-API-Key": key_a, "Idempotency-Key": f"create-{suffix}"}
        payload = {"input": {"prompt": "SEC-E1 private test"}}
        response = client.post("/v1/runs", headers=owner, json=payload)
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        # A known persisted result lets owner evaluation succeed without provider access.
        conn.execute(
            "UPDATE runs SET execution_status='SUCCEEDED', result_payload=%s WHERE id=%s",
            (json.dumps({"answer": "ready"}), run_id),
        )
        conn.commit()
        attacker = {
            "X-API-Key": key_b,
            "X-Client-Id": tenant_a,
            "Idempotency-Key": f"replay-{suffix}",
        }
        for suffix_path in ("", "/attempts", "/events", "/evaluations"):
            assert (
                client.get(f"/v1/runs/{run_id}{suffix_path}", headers=attacker).status_code == 404
            )
            assert client.get(f"/v1/runs/{run_id}{suffix_path}", headers=owner).status_code == 200
        rules = {"rules": [{"type": "non_empty", "path": "answer"}]}
        assert (
            client.post(f"/v1/runs/{run_id}/evaluations", headers=attacker, json=rules).status_code
            == 404
        )
        assert (
            client.post(f"/v1/runs/{run_id}/evaluations", headers=owner, json=rules).status_code
            == 200
        )
        assert client.post(f"/v1/runs/{run_id}/replay", headers=attacker).status_code == 404
        assert (
            client.post(
                f"/v1/runs/{run_id}/replay",
                headers={**owner, "Idempotency-Key": f"replay-{suffix}"},
            ).status_code
            == 202
        )
        other = client.post(
            "/v1/runs", headers={**owner, "X-API-Key": key_b, "X-Client-Id": tenant_a}, json=payload
        )
        assert other.status_code == 202 and other.json()["run_id"] != run_id
        duplicate = client.post(
            "/v1/runs",
            headers={**owner, "X-API-Key": key_new, "X-Client-Id": tenant_b},
            json=payload,
        )
        assert duplicate.status_code == 202 and duplicate.json()["run_id"] == run_id
        assert duplicate.json()["replayed"]
        assert (
            client.get(
                f"/v1/runs/{run_id}", headers={"X-API-Key": key_a, "X-Client-Id": "x" * 129}
            ).status_code
            == 422
        )
        stored_owner = conn.execute("SELECT client_id FROM runs WHERE id=%s", (run_id,)).fetchone()
        assert stored_owner == (tenant_a,)
        rows = conn.execute(
            "SELECT client_id, credential_fingerprint FROM security_audit_events "
            "WHERE created_at >= %s",
            (started,),
        ).fetchall()
        assert rows
        assert all(
            tenant in {tenant_a, tenant_b} and len(fingerprint) == 16
            for tenant, fingerprint in rows
        )
        rendered = json.dumps(rows)
        for secret in (key_a, key_b, key_new, pepper, record_a.verifier.get_secret_value()):
            assert secret not in rendered

    # Both rotations share the already-consumed principal budget, across API instances.
    limited = Settings(**{**settings.model_dump(), "rate_limit_requests": 1})
    with TestClient(create_app(limited)) as client:
        assert client.get(f"/v1/runs/{run_id}", headers={"X-API-Key": key_new}).status_code == 429


async def test_real_redis_atomic_concurrency_expiry_and_orphan_repair() -> None:
    assert REDIS_URL
    principal = f"sec-e1-concurrency-{uuid.uuid4().hex}"
    key = f"arr:rate-limit:{hashlib.sha256(principal.encode()).hexdigest()}"
    limiter = RedisFixedWindowRateLimiter(redis_url=REDIS_URL, limit=7, window_seconds=60)
    store = Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        decisions = await asyncio.gather(*(limiter.check(principal) for _ in range(50)))
        assert sum(decision.allowed for decision in decisions) == 7
        assert int(await store.get(key)) == 50
        assert 0 < await store.ttl(key) <= 60
        await store.persist(key)
        assert not (await limiter.check(principal)).allowed
        assert 0 < await store.ttl(key) <= 60
        # Expire only this randomly generated test key; next request opens a fresh window.
        await store.pexpire(key, 1)
        await asyncio.sleep(0.02)
        assert (await limiter.check(principal)).allowed
        assert int(await store.get(key)) == 1
    finally:
        await store.delete(key)
        await store.aclose()
        await limiter.close()
