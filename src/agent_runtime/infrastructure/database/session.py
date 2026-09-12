from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from agent_runtime.settings import Settings


def create_database_engine(settings: Settings) -> AsyncEngine:
    """Build the engine with an explicitly sized, overflow-free pool.

    Two separate reasons, both measured.

    Size (PERF-009): SQLAlchemy's defaults (5 + 10 overflow) are per process,
    and this runtime runs four of them. With the chart's default replica counts
    that is already 90 of PostgreSQL's default 100 connections, which leaves no
    headroom to scale the API at all.

    Overflow (PERF-011): an overflow connection is opened when the pool is empty
    and closed when it is returned, so sustained load just above ``pool_size``
    establishes a new PostgreSQL session every few requests. Measured over 20 s
    at 10 VUs: 1,148 new sessions and 6,936 requests with overflow enabled,
    against 10 sessions and 15,787 requests with it disabled. Requests now queue
    for a pooled connection instead, bounded by ``pool_timeout``.
    """

    return create_async_engine(
        str(settings.database_url),
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
