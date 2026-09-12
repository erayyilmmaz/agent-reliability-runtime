"""PostgreSQL-authoritative quotas, shared by API replicas, jobs and retries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from agent_runtime.application.runs import QuotaExceededError as QuotaExceededError
from agent_runtime.infrastructure.database.models import ProviderQuota, Run
from agent_runtime.observability.metrics import get_runtime_metrics

if TYPE_CHECKING:
    from agent_runtime.settings import Settings


@dataclass(frozen=True)
class QuotaLimits:
    principal_active: int = 20
    tenant_active: int = 40
    principal_calls: int = 120
    tenant_calls: int = 240
    window_seconds: int = 3600
    principal_running: int = 1
    tenant_running: int = 2

    @classmethod
    def from_settings(cls, settings: Settings) -> QuotaLimits:
        return cls(
            settings.principal_concurrent_runs,
            settings.tenant_concurrent_runs,
            settings.principal_provider_calls,
            settings.tenant_provider_calls,
            settings.quota_window_seconds,
            settings.principal_running_runs,
            settings.tenant_running_runs,
        )


async def lock_identity(session: AsyncSession, principal: str, tenant: str) -> None:
    for scope in sorted((f"p:{principal}", f"t:{tenant}")):
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"), {"scope": scope}
        )


async def check_admission(
    session: AsyncSession, principal: str, tenant: str, limits: QuotaLimits
) -> None:
    # Caller holds both identity locks until its new Run/outbox transaction commits.
    active = ("QUEUED", "RUNNING", "RETRY_SCHEDULED")
    for condition, limit in (
        (Run.principal_id == principal, limits.principal_active),
        (Run.client_id == tenant, limits.tenant_active),
    ):
        count = await session.scalar(
            select(func.count()).select_from(Run).where(condition, Run.execution_status.in_(active))
        )
        if int(count or 0) >= limit:
            raise QuotaExceededError("Active work quota exceeded")


async def charge_provider_call(
    session: AsyncSession, principal: str, tenant: str, limits: QuotaLimits
) -> None:
    await lock_identity(session, principal, tenant)
    now = int(await session.scalar(select(func.extract("epoch", func.clock_timestamp()))) or 0)
    window = now // limits.window_seconds * limits.window_seconds
    rows = []
    for scope, limit in (
        (f"p:{principal}", limits.principal_calls),
        (f"t:{tenant}", limits.tenant_calls),
    ):
        row = await session.get(ProviderQuota, scope)
        if row is None:
            row = ProviderQuota(scope=scope, window_start=window, used=0)
            session.add(row)
        if row.window_start != window:
            row.window_start, row.used = window, 0
        if row.used >= limit:
            get_runtime_metrics().security_event("quota", "denied")
            raise QuotaExceededError("Provider-call quota exceeded")
        rows.append(row)
    for row in rows:
        row.used += 1
