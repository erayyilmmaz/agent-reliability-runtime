"""Durable, append-only security audit records with no request bodies or raw keys."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_runtime.infrastructure.database.models import SecurityAuditEvent


@dataclass(frozen=True)
class SecurityAuditRecord:
    event_type: str
    outcome: str
    reason: str
    client_id: str | None
    credential_fingerprint: str | None


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
                        event_type=record.event_type,
                        outcome=record.outcome,
                        reason=record.reason,
                        client_id=record.client_id,
                        credential_fingerprint=record.credential_fingerprint,
                    )
                )


class NoopSecurityAuditSink:
    async def record(self, record: SecurityAuditRecord) -> None:
        del record
