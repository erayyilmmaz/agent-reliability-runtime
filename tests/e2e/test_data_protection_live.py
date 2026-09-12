"""Disposable SQL only: no production key store and no existing payloads touched."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import select, text
from test_resource_controls_live import DB, REDIS, migrate, profile  # noqa: F401

from agent_runtime.domain.retry import RetryPolicy
from agent_runtime.infrastructure.database.models import Run, SecurityAuditEvent, TenantKey
from agent_runtime.infrastructure.database.session import (
    create_database_engine,
    create_session_factory,
)
from agent_runtime.security.audit import SecurityAuditRecord, SqlAlchemySecurityAuditSink
from agent_runtime.security.envelope import (
    KeyDestroyedError,
    LocalKeyWrapper,
    TenantEnvelope,
    erase_tenant_key,
    retention_candidates,
)

pytestmark = pytest.mark.skipif(not DB or not REDIS, reason="Requires disposable SQL and Redis")


async def test_retention_operator_preview_uses_configured_days_without_deleting(
    monkeypatch, capsys
):
    import argparse

    from agent_runtime.security import retention_cli

    settings, _, _ = profile(payload_retention_days=3650)
    monkeypatch.setattr(retention_cli, "get_settings", lambda: settings)
    await retention_cli._run(
        argparse.Namespace(
            apply=False, tenant_id=None, confirm_tenant=None, principal_id="preview-test"
        )
    )
    assert json.loads(capsys.readouterr().out) == {"dry_run": True, "candidates": []}


async def test_audit_sink_truncates_at_real_database_boundary_and_stays_immutable():
    settings, _, _ = profile()
    engine = create_database_engine(settings)
    sessions = create_session_factory(engine)
    target = uuid4()
    try:
        await SqlAlchemySecurityAuditSink(sessions).record(
            SecurityAuditRecord(
                "SENSITIVE_READ",
                "ALLOWED",
                "r" * 1000,
                "t" * 1000,
                None,
                principal_id="p" * 1000,
                target_run_id=target,
                resource="run",
            )
        )
        async with sessions() as session:
            record = await session.scalar(
                select(SecurityAuditEvent).where(SecurityAuditEvent.target_run_id == target)
            )
            assert (
                len(record.reason) == 64
                and len(record.client_id) == len(record.principal_id) == 128
            )
            with pytest.raises(Exception, match="append-only"):
                await session.execute(
                    text("UPDATE security_audit_events SET reason='edited' WHERE id=:id"),
                    {"id": record.id},
                )
            await session.rollback()
    finally:
        await engine.dispose()


async def test_envelope_binding_concurrent_key_creation_and_explicit_erasure():
    settings, _, _ = profile()
    engine = create_database_engine(settings)
    sessions = create_session_factory(engine)
    cipher = TenantEnvelope(LocalKeyWrapper(os.urandom(32)))
    tenant, run_id = "t-" + uuid4().hex, uuid4()
    try:

        async def encrypt():
            async with sessions.begin() as session:
                return await cipher.encrypt(
                    session,
                    tenant_id=tenant,
                    run_id=run_id,
                    field="input",
                    value={"prompt": "private data"},
                )

        encrypted = await asyncio.gather(*(encrypt() for _ in range(8)))
        assert len({v["data"] for v in encrypted}) == 8
        assert "private data" not in json.dumps(encrypted)
        async with sessions.begin() as session:
            assert await cipher.decrypt(
                session, tenant_id=tenant, run_id=run_id, field="input", envelope=encrypted[0]
            ) == {"prompt": "private data"}
            with pytest.raises(InvalidTag):
                await cipher.decrypt(
                    session, tenant_id=tenant, run_id=uuid4(), field="input", envelope=encrypted[0]
                )
            with pytest.raises(InvalidTag):
                await cipher.decrypt(
                    session, tenant_id=tenant, run_id=run_id, field="result", envelope=encrypted[0]
                )
            policy = await cipher.protect_policy(
                session,
                tenant_id=tenant,
                run_id=run_id,
                policy={"max_attempts": 2, "instructions": "private instruction"},
            )
            assert RetryPolicy.from_snapshot(policy).max_attempts == 2
            assert "instructions" not in policy and "private instruction" not in json.dumps(policy)
            assert await erase_tenant_key(session, tenant_id=tenant, principal_id="operator")
            assert await cipher.decrypt(
                session, tenant_id=tenant, run_id=run_id, field="input", envelope=encrypted[0]
            ) == {"prompt": "private data"}
        async with sessions.begin() as session:
            with pytest.raises(ValueError, match="confirmation"):
                await erase_tenant_key(
                    session, tenant_id=tenant, principal_id="operator", dry_run=False
                )
        async with sessions.begin() as session:
            assert await erase_tenant_key(
                session,
                tenant_id=tenant,
                principal_id="operator",
                dry_run=False,
                confirm_tenant=tenant,
            )
        async with sessions.begin() as session:
            row = await session.get(TenantKey, tenant)
            assert row.wrapped_dek is None and row.destroyed_at is not None
            with pytest.raises(KeyDestroyedError):
                await cipher.decrypt(
                    session, tenant_id=tenant, run_id=run_id, field="input", envelope=encrypted[0]
                )
            with pytest.raises(KeyDestroyedError):
                await cipher.encrypt(
                    session, tenant_id=tenant, run_id=run_id, field="input", value={}
                )
            assert not await erase_tenant_key(
                session,
                tenant_id=tenant,
                principal_id="operator",
                dry_run=False,
                confirm_tenant=tenant,
            )
    finally:
        await engine.dispose()


async def test_retention_excludes_active_and_fresh_tenants_and_rechecks_at_erasure():
    settings, _, _ = profile()
    engine = create_database_engine(settings)
    sessions = create_session_factory(engine)
    cipher = TenantEnvelope(LocalKeyWrapper(os.urandom(32)))
    prefix = uuid4().hex
    tenants = [f"ret-{prefix}-{kind}" for kind in ("old", "active", "fresh")]
    cutoff = datetime.now(UTC) - timedelta(days=30)
    try:
        for tenant in tenants:
            async with sessions.begin() as session:
                await cipher.encrypt(
                    session, tenant_id=tenant, run_id=uuid4(), field="result", value={"ok": True}
                )
                key = await session.get(TenantKey, tenant)
                key.last_encrypted_at = cutoff - timedelta(days=1)
                session.add(
                    Run(
                        client_id=tenant,
                        idempotency_key="retention-fixture",
                        request_hash="0" * 64,
                        input_payload={},
                        policy_snapshot={},
                        execution_status="QUEUED" if tenant == tenants[1] else "SUCCEEDED",
                        completed_at=None
                        if tenant == tenants[1]
                        else datetime.now(UTC)
                        if tenant == tenants[2]
                        else cutoff - timedelta(days=1),
                    )
                )
        async with sessions.begin() as session:
            eligible = await retention_candidates(session, cutoff=cutoff)
            assert tenants[0] in eligible and not set(tenants[1:]) & set(eligible)
            for tenant in tenants[1:]:
                assert not await erase_tenant_key(
                    session,
                    tenant_id=tenant,
                    principal_id="operator",
                    dry_run=False,
                    confirm_tenant=tenant,
                    cutoff=cutoff,
                )
            assert await erase_tenant_key(
                session, tenant_id=tenants[0], principal_id="operator", cutoff=cutoff
            )
            assert (await session.get(TenantKey, tenants[0])).destroyed_at is None
    finally:
        await engine.dispose()
