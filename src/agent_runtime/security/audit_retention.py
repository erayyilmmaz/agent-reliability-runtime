"""Age rows out of the append-only security audit table (PERF-005).

``security_audit_events`` grows with authenticated read traffic and nothing
removes a row, so without retention the table is unbounded. Two properties have
to survive that:

*Immutability.* The append-only trigger still refuses every UPDATE and every
DELETE. A purge transaction opts out for itself by setting
``arr.allow_audit_purge = 'on'`` with ``SET LOCAL``, which lasts exactly one
transaction and takes no table-level lock. The alternative --
``ALTER TABLE ... DISABLE TRIGGER`` -- takes ``ShareRowExclusiveLock`` and was
measured blocking a concurrent audit INSERT for 4.0 s against a 6 s purge; audit
writes are on the request path, so that is an API stall. Under this gate the same
concurrent INSERT measured 1.7 ms against a 0.9 ms unloaded baseline.

*Availability.* Deletes run in bounded batches, each in its own short
transaction, so no single statement holds row locks or a snapshot for long.

This is operator tooling, not a scheduled job. It is dry-run by default, does
nothing at all unless ``APP_AUDIT_RETENTION_DAYS`` is set, and connects with the
DDL credential: per SEC-018 the runtime role holds INSERT only on this table and
cannot delete regardless of the gate. The grant is the boundary; the gate only
means the owner cannot delete audit history by accident.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from agent_runtime.settings import Settings, get_settings

PURGE_GUC = "arr.allow_audit_purge"

_COUNT = text("SELECT count(*) FROM security_audit_events WHERE created_at < :cutoff")
# The CTE bounds the statement: ctid is the cheapest join back to the heap, and
# the ORDER BY keeps the scan on ix_security_audit_events_created_at.
_DELETE_BATCH = text(
    """
    WITH doomed AS (
        SELECT ctid
        FROM security_audit_events
        WHERE created_at < :cutoff
        ORDER BY created_at
        LIMIT :batch_size
    )
    DELETE FROM security_audit_events
    WHERE ctid IN (SELECT ctid FROM doomed)
    """
)


class RetentionDisabledError(RuntimeError):
    """Raised when a purge is requested without a configured horizon."""


@dataclass
class PurgeReport:
    dry_run: bool
    cutoff: datetime
    retention_days: int
    batch_size: int
    eligible: int
    deleted: int = 0
    batches: list[int] = field(default_factory=list)
    stopped_early: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "cutoff": self.cutoff.isoformat(),
            "retention_days": self.retention_days,
            "batch_size": self.batch_size,
            "eligible": self.eligible,
            "deleted": self.deleted,
            "batches": self.batches,
            "stopped_early": self.stopped_early,
        }


def resolve_cutoff(settings: Settings, *, now: datetime | None = None) -> datetime:
    """The age boundary, or raise if the operator has not opted in."""

    if settings.audit_retention_days is None:
        raise RetentionDisabledError(
            "Audit retention is disabled; set APP_AUDIT_RETENTION_DAYS to enable it."
        )
    return (now or datetime.now(UTC)) - timedelta(days=settings.audit_retention_days)


def create_owner_engine(settings: Settings) -> AsyncEngine:
    """Connect as the table owner; the runtime role cannot delete (SEC-018)."""

    return create_async_engine(
        str(settings.effective_migration_database_url),
        pool_pre_ping=True,
        pool_size=1,
        max_overflow=0,
    )


async def count_expired(session: AsyncSession, *, cutoff: datetime) -> int:
    return int((await session.execute(_COUNT, {"cutoff": cutoff})).scalar_one())


async def purge_batch(session: AsyncSession, *, cutoff: datetime, batch_size: int) -> int:
    """Delete up to ``batch_size`` expired rows within the caller's transaction.

    ``SET LOCAL`` binds the gate to this transaction, so a caller that rolls
    back never leaves the table deletable.
    """

    await session.execute(text(f"SET LOCAL {PURGE_GUC} = 'on'"))
    result = cast(
        "CursorResult[Any]",
        await session.execute(_DELETE_BATCH, {"cutoff": cutoff, "batch_size": batch_size}),
    )
    return int(result.rowcount or 0)


async def purge_expired_audit_events(
    engine: AsyncEngine,
    *,
    settings: Settings,
    dry_run: bool = True,
    max_batches: int | None = None,
    pause_seconds: float = 0.0,
    now: datetime | None = None,
) -> PurgeReport:
    cutoff = resolve_cutoff(settings, now=now)
    batch_size = settings.audit_purge_batch_size

    async with AsyncSession(bind=engine) as session:
        eligible = await count_expired(session, cutoff=cutoff)

    report = PurgeReport(
        dry_run=dry_run,
        cutoff=cutoff,
        retention_days=int(settings.audit_retention_days or 0),
        batch_size=batch_size,
        eligible=eligible,
    )
    if dry_run or eligible == 0:
        return report

    batches = 0
    while max_batches is None or batches < max_batches:
        # One transaction per batch: the gate, the delete and the commit stay
        # together, and nothing holds locks between batches.
        async with AsyncSession(bind=engine) as session:
            async with session.begin():
                removed = await purge_batch(session, cutoff=cutoff, batch_size=batch_size)
        if removed == 0:
            break
        report.deleted += removed
        report.batches.append(removed)
        batches += 1
        if pause_seconds:
            await asyncio.sleep(pause_seconds)
    else:
        report.stopped_early = True

    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Age expired rows out of security_audit_events. Dry-run by default.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete. Without it nothing is removed.",
    )
    parser.add_argument(
        "--confirm-retention-days",
        type=int,
        help="Must equal APP_AUDIT_RETENTION_DAYS; required with --apply.",
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        help="Stop after this many batches so a first run can be bounded.",
    )
    parser.add_argument(
        "--pause-seconds",
        type=float,
        default=0.0,
        help="Idle between batches to leave headroom for request-path writes.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    settings = get_settings()

    if args.apply:
        # Deletion is irreversible on an append-only table: make the operator
        # restate the horizon rather than trust an inherited environment.
        if settings.audit_retention_days is None:
            parser.error("--apply requires APP_AUDIT_RETENTION_DAYS to be set")
        if args.confirm_retention_days != settings.audit_retention_days:
            parser.error(
                "--apply requires --confirm-retention-days to match APP_AUDIT_RETENTION_DAYS"
            )

    try:
        asyncio.run(_run(args, settings))
    except RetentionDisabledError as exc:
        # Operator tooling: say what to set, do not print a traceback.
        parser.error(str(exc))


async def _run(args: argparse.Namespace, settings: Settings) -> None:
    engine = create_owner_engine(settings)
    try:
        report = await purge_expired_audit_events(
            engine,
            settings=settings,
            dry_run=not args.apply,
            max_batches=args.max_batches,
            pause_seconds=args.pause_seconds,
        )
    finally:
        await engine.dispose()
    print(json.dumps(report.as_dict(), sort_keys=True))
