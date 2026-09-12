from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from agent_runtime.settings import Settings


def create_database_engine(settings: Settings) -> AsyncEngine:
    """Build the engine with an explicitly sized pool.

    SQLAlchemy's defaults (5 + 10 overflow) are per process, and this runtime
    runs four of them. With the chart's default replica counts that is already
    90 of PostgreSQL's default 100 connections, which leaves no headroom to
    scale the API at all (PERF-009).
    """

    return create_async_engine(
        str(settings.database_url),
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
