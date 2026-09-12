"""Durable, append-only security audit records with no request bodies or raw keys."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_runtime.infrastructure.database.models import SecurityAuditEvent


@dataclass(frozen=True)
class SecurityAuditRecord:
    event_type: str
    outcome: str
    reason: str
    client_id: str | None
    credential_fingerprint: str | None
    principal_id: str | None = None
    target_run_id: UUID | None = None
    resource: str | None = None


class SecurityAuditSink(Protocol):
    async def record(self, record: SecurityAuditRecord) -> None: ...


class SqlAlchemySecurityAuditSink:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(self, record: SecurityAuditRecord) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                session.add(
                    SecurityAuditEvent(
                        event_type=safe_text(record.event_type, 64),
                        outcome=safe_text(record.outcome, 16),
                        reason=safe_text(record.reason, 64),
                        client_id=safe_text(record.client_id, 128),
                        credential_fingerprint=safe_text(record.credential_fingerprint, 64),
                        principal_id=safe_text(record.principal_id, 128),
                        target_run_id=record.target_run_id,
                        resource=record.resource
                        if record.resource in {"run", "attempts", "events", "evaluations", "job"}
                        else None,
                    )
                )


class NoopSecurityAuditSink:
    async def record(self, record: SecurityAuditRecord) -> None:
        del record


def safe_text(value: str | None, limit: int) -> str | None:
    """Truncate at the persistence boundary and reject control/surrogate characters."""
    if value is None:
        return None
    return "".join(c if 32 <= ord(c) < 127 else "_" for c in value[:limit])
