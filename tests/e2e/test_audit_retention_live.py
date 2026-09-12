"""Audit retention against real SQL: the gate must age rows out without ever
making the table mutable outside the purge transaction (PERF-005)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from test_resource_controls_live import DB, REDIS, migrate, profile  # noqa: F401

from agent_runtime.infrastructure.database.models import SecurityAuditEvent
from agent_runtime.security.audit_retention import (
    RetentionDisabledError,
    create_owner_engine,
    purge_expired_audit_events,
    resolve_cutoff,
)

pytestmark = pytest.mark.skipif(not DB or not REDIS, reason="Requires disposable SQL and Redis")


async def _seed(engine, *, marker: str, age_days: int, count: int) -> None:
    created = datetime.now(UTC) - timedelta(days=age_days)
    async with engine.begin() as connection:
        for _ in range(count):
            await connection.execute(
                text(
                    "INSERT INTO security_audit_events "
                    "(id, event_type, outcome, reason, created_at) "
                    "VALUES (:id, :event_type, 'ALLOWED', :reason, :created_at)"
                ),
                {
                    "id": uuid4(),
                    "event_type": "SENSITIVE_READ",
                    "reason": marker,
                    "created_at": created,
                },
            )


async def _count(engine, marker: str) -> int:
    async with engine.connect() as connection:
        return int(
            (
                await connection.execute(
                    select(func.count())
                    .select_from(SecurityAuditEvent)
                    .where(SecurityAuditEvent.reason == marker)
                )
            ).scalar_one()
        )


def test_retention_is_disabled_until_an_operator_sets_a_horizon():
    settings, _, _ = profile()
    assert settings.audit_retention_days is None
    with pytest.raises(RetentionDisabledError):
        resolve_cutoff(settings)


async def test_purge_removes_only_expired_rows_and_leaves_the_table_immutable():
    marker = f"purge-{uuid4().hex}"
    fresh = f"fresh-{uuid4().hex}"
    settings, _, _ = profile(audit_retention_days=30, audit_purge_batch_size=40)
    engine = create_owner_engine(settings)
    try:
        # The purge is global by design, so exact batch assertions need a known
        # starting point: drain whatever earlier tests left behind first.
        await purge_expired_audit_events(engine, settings=settings, dry_run=False)
        await _seed(engine, marker=marker, age_days=400, count=100)
        await _seed(engine, marker=fresh, age_days=1, count=10)

        preview = await purge_expired_audit_events(engine, settings=settings)
        assert preview.dry_run is True
        assert preview.deleted == 0
        assert await _count(engine, marker) == 100, "dry-run must not delete"

        report = await purge_expired_audit_events(engine, settings=settings, dry_run=False)
        assert report.deleted == 100
        # Bounded batches, not one large statement: this is what keeps the
        # request path unblocked.
        assert report.batches == [40, 40, 20]
        assert await _count(engine, marker) == 0
        assert await _count(engine, fresh) == 10, "rows inside the horizon must survive"

        # The gate is SET LOCAL, so it expired with the purge transaction.
        async with engine.begin() as connection:
            with pytest.raises(Exception, match="append-only"):
                await connection.execute(
                    text("DELETE FROM security_audit_events WHERE reason = :r"), {"r": fresh}
                )
        async with engine.begin() as connection:
            with pytest.raises(Exception, match="append-only"):
                await connection.execute(
                    text("UPDATE security_audit_events SET outcome = 'X' WHERE reason = :r"),
                    {"r": fresh},
                )
        assert await _count(engine, fresh) == 10
    finally:
        await engine.dispose()


async def test_max_batches_bounds_a_first_run_and_reports_that_it_stopped():
    marker = f"bounded-{uuid4().hex}"
    settings, _, _ = profile(audit_retention_days=30, audit_purge_batch_size=25)
    engine = create_owner_engine(settings)
    try:
        await purge_expired_audit_events(engine, settings=settings, dry_run=False)
        await _seed(engine, marker=marker, age_days=400, count=100)
        report = await purge_expired_audit_events(
            engine, settings=settings, dry_run=False, max_batches=2
        )
        assert report.deleted == 50
        assert report.stopped_early is True
        assert await _count(engine, marker) == 50
    finally:
        await engine.dispose()


async def test_purge_does_not_block_a_concurrent_audit_insert():
    """The reason the gate exists.

    ``ALTER TABLE ... DISABLE TRIGGER`` takes ShareRowExclusiveLock, which
    conflicts with the RowExclusiveLock an INSERT needs. The GUC gate takes no
    table-level lock, so an audit write issued while a purge transaction is open
    must not wait on it.
    """

    marker = f"lock-{uuid4().hex}"
    settings, _, _ = profile(audit_retention_days=30, audit_purge_batch_size=500)
    engine = create_owner_engine(settings)
    writer = create_owner_engine(settings)
    try:
        await purge_expired_audit_events(engine, settings=settings, dry_run=False)
        await _seed(engine, marker=marker, age_days=400, count=500)
        cutoff = resolve_cutoff(settings)
        async with engine.begin() as purge:
            await purge.execute(text("SET LOCAL arr.allow_audit_purge = 'on'"))
            await purge.execute(
                text("DELETE FROM security_audit_events WHERE created_at < :c"),
                {"c": cutoff},
            )
            # Purge transaction still open and holding its locks here.
            async with writer.begin() as connection:
                modes = set(
                    (
                        await connection.execute(
                            text(
                                "SELECT DISTINCT mode FROM pg_locks "
                                "WHERE relation = 'security_audit_events'::regclass"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                # RowExclusiveLock is self-compatible, so writers proceed. A
                # regression to ALTER TABLE ... DISABLE TRIGGER would show up
                # here as ShareRowExclusiveLock.
                assert modes == {"RowExclusiveLock"}, modes

                # Without this a regression would hang the suite instead of
                # failing it.
                await connection.execute(text("SET LOCAL lock_timeout = '5s'"))
                await connection.execute(
                    text(
                        "INSERT INTO security_audit_events "
                        "(id, event_type, outcome, reason) "
                        "VALUES (:id, 'SENSITIVE_READ', 'ALLOWED', :r)"
                    ),
                    {"id": uuid4(), "r": f"{marker}-concurrent"},
                )
        assert await _count(engine, f"{marker}-concurrent") == 1
    finally:
        await engine.dispose()
        await writer.dispose()


def test_owner_engine_uses_the_ddl_credential_not_the_runtime_one():
    """SEC-018: the runtime role holds INSERT only and cannot purge."""

    owner_url = "postgresql+asyncpg://arr_migrator:pw@localhost:5432/agent_runtime"
    settings, _, _ = profile(migration_database_url=owner_url)
    engine = create_owner_engine(settings)
    try:
        assert engine.url.username == "arr_migrator"
    finally:
        engine.sync_engine.dispose()

    # Falls back to the runtime credential only when no DDL role is configured.
    fallback, _, _ = profile()
    assert str(fallback.effective_migration_database_url) == str(fallback.database_url)
