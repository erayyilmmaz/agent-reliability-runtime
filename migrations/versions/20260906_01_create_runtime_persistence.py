"""Create durable runtime persistence tables.

Revision ID: 20260906_01
Revises:
Create Date: 2026-09-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260906_01"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    json_type = postgresql.JSONB(astext_type=sa.Text())

    op.create_table(
        "runs",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("client_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("input_payload", json_type, nullable=False),
        sa.Column("policy_snapshot", json_type, nullable=False),
        sa.Column("execution_status", sa.String(length=32), nullable=False),
        sa.Column("evaluation_status", sa.String(length=32), nullable=False),
        sa.Column("result_payload", json_type, nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replay_of_run_id", uuid_type, nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint(
            "execution_status IN ('QUEUED', 'RUNNING', 'RETRY_SCHEDULED', "
            "'SUCCEEDED', 'FAILED', 'DEAD_LETTERED')",
            name="ck_runs_execution_status",
        ),
        sa.CheckConstraint(
            "evaluation_status IN ('NOT_RUN', 'PENDING', 'PASSED', 'FAILED', 'ERROR')",
            name="ck_runs_evaluation_status",
        ),
        sa.ForeignKeyConstraint(["replay_of_run_id"], ["runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id", "idempotency_key", name="uq_runs_client_idempotency_key"),
    )
    op.create_index("ix_runs_client_id", "runs", ["client_id"])
    op.create_index("ix_runs_replay_of_run_id", "runs", ["replay_of_run_id"])

    op.create_table(
        "run_attempts",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("run_id", uuid_type, nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("usage_metadata", json_type, nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "attempt_number", name="uq_run_attempts_run_number"),
    )
    op.create_index("ix_run_attempts_run_id", "run_attempts", ["run_id"])

    op.create_table(
        "run_events",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("run_id", uuid_type, nullable=False),
        sa.Column("attempt_id", uuid_type, nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("metadata", json_type, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["attempt_id"], ["run_attempts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_run_events_run_id", "run_events", ["run_id"])
    op.create_index("ix_run_events_attempt_id", "run_events", ["attempt_id"])

    op.create_table(
        "outbox_events",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("aggregate_id", uuid_type, nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", json_type, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("publish_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_outbox_events_aggregate_id", "outbox_events", ["aggregate_id"])

    op.create_table(
        "evaluations",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("run_id", uuid_type, nullable=False),
        sa.Column("evaluator", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("result", json_type, nullable=True),
        sa.Column("details", json_type, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('NOT_RUN', 'PENDING', 'PASSED', 'FAILED', 'ERROR')",
            name="ck_evaluations_status",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_evaluations_run_id", "evaluations", ["run_id"])

    op.execute(
        """
        CREATE FUNCTION prevent_run_event_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'run_events are append-only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER run_events_no_update_or_delete BEFORE UPDATE OR DELETE ON run_events "
        "FOR EACH ROW EXECUTE FUNCTION prevent_run_event_mutation();"
    )
    op.execute(
        """
        CREATE FUNCTION prevent_run_attempt_delete() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'run_attempts are retained history and cannot be deleted';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER run_attempts_no_delete BEFORE DELETE ON run_attempts "
        "FOR EACH ROW EXECUTE FUNCTION prevent_run_attempt_delete();"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER run_attempts_no_delete ON run_attempts")
    op.execute("DROP FUNCTION prevent_run_attempt_delete")
    op.execute("DROP TRIGGER run_events_no_update_or_delete ON run_events")
    op.execute("DROP FUNCTION prevent_run_event_mutation")
    op.drop_index("ix_evaluations_run_id", table_name="evaluations")
    op.drop_table("evaluations")
    op.drop_index("ix_outbox_events_aggregate_id", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_index("ix_run_events_attempt_id", table_name="run_events")
    op.drop_index("ix_run_events_run_id", table_name="run_events")
    op.drop_table("run_events")
    op.drop_index("ix_run_attempts_run_id", table_name="run_attempts")
    op.drop_table("run_attempts")
    op.drop_index("ix_runs_replay_of_run_id", table_name="runs")
    op.drop_index("ix_runs_client_id", table_name="runs")
    op.drop_table("runs")
