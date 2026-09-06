from agent_runtime.infrastructure.database.models import Base, Run


def test_metadata_contains_every_durable_runtime_entity() -> None:
    assert set(Base.metadata.tables) == {
        "runs",
        "run_attempts",
        "run_events",
        "outbox_events",
        "evaluations",
    }


def test_run_uses_optimistic_concurrency_version_column() -> None:
    assert Run.__mapper__.version_id_col is Run.__table__.c.version


def test_run_idempotency_is_scoped_per_client() -> None:
    unique_constraints = [
        constraint
        for constraint in Run.__table__.constraints
        if constraint.name == "uq_runs_client_idempotency_key"
    ]

    assert len(unique_constraints) == 1
    assert [column.name for column in unique_constraints[0].columns] == [
        "client_id",
        "idempotency_key",
    ]


def test_run_has_persisted_next_attempt_timestamp() -> None:
    assert "next_attempt_at" in Run.__table__.c


def test_run_persists_trace_context_for_retries_and_fallbacks() -> None:
    assert "trace_context" in Run.__table__.c
