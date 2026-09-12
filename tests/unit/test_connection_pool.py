"""PERF-009: the pool is sized explicitly because every process opens its own."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_runtime.infrastructure.database.session import create_database_engine
from agent_runtime.settings import Settings


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"environment": "local", "auth_mode": "disabled"}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_pool_is_sized_from_settings_not_sqlalchemy_defaults() -> None:
    """SQLAlchemy would default to 5+10; four processes of that exhaust the DB."""
    pool = create_database_engine(_settings()).pool

    assert pool.size() == 5
    assert pool._max_overflow == 5


def test_pool_size_is_configurable() -> None:
    pool = create_database_engine(_settings(db_pool_size=3, db_max_overflow=2)).pool

    assert pool.size() == 3
    assert pool._max_overflow == 2


def test_cluster_connection_ceiling_fits_postgres_defaults() -> None:
    """The chart's defaults must leave room for the API autoscaler.

    (api_max + worker + dispatcher + scheduler) x (pool + overflow) has to stay
    under PostgreSQL's default max_connections of 100. Before PERF-009 the
    shipped defaults already consumed 90 of them, so enabling any autoscaling
    would have exhausted the database rather than serving more traffic.
    """
    settings = _settings()
    per_process = settings.db_pool_size + settings.db_max_overflow

    api_max_replicas = 4  # charts/.../values.yaml api.autoscaling.maxReplicas
    processes = api_max_replicas + 2 + 1 + 1  # worker, dispatcher, scheduler

    assert processes * per_process < 100


@pytest.mark.parametrize("field,value", [("db_pool_size", 0), ("db_max_overflow", -1)])
def test_pool_bounds_are_enforced(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        _settings(**{field: value})
