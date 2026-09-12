"""Durable job identity and principal/tenant resource budgets."""

import sqlalchemy as sa
from alembic import op

revision = "20260912_07"
down_revision = "20260906_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("principal_id", sa.String(128), nullable=True))
    op.add_column(
        "runs", sa.Column("work_kind", sa.String(16), nullable=False, server_default="execution")
    )
    op.create_check_constraint(
        "ck_runs_work_kind", "runs", "work_kind IN ('execution', 'evaluation', 'regression')"
    )
    op.create_index("ix_runs_principal_id", "runs", ["principal_id"])
    op.create_index("ix_runs_principal_active", "runs", ["principal_id", "execution_status"])
    op.create_index("ix_runs_tenant_active", "runs", ["client_id", "execution_status"])
    op.add_column("evaluations", sa.Column("job_run_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_evaluations_job_run", "evaluations", "runs", ["job_run_id"], ["id"], ondelete="RESTRICT"
    )
    op.create_unique_constraint("uq_evaluations_job_run", "evaluations", ["job_run_id"])
    op.create_table(
        "provider_quotas",
        sa.Column("scope", sa.String(160), primary_key=True),
        sa.Column("window_start", sa.Integer(), nullable=False),
        sa.Column("used", sa.Integer(), nullable=False),
        sa.CheckConstraint("used >= 0", name="ck_provider_quota_used"),
    )
    for table, timestamp in (
        ("run_attempts", "started_at"),
        ("run_events", "created_at"),
        ("evaluations", "created_at"),
    ):
        op.create_index(f"ix_{table}_page", table, ["run_id", timestamp, "id"])


def downgrade() -> None:
    for table in ("run_attempts", "run_events", "evaluations"):
        op.drop_index(f"ix_{table}_page", table_name=table)
    op.drop_table("provider_quotas")
    op.drop_constraint("uq_evaluations_job_run", "evaluations", type_="unique")
    op.drop_constraint("fk_evaluations_job_run", "evaluations", type_="foreignkey")
    op.drop_column("evaluations", "job_run_id")
    for index in ("ix_runs_principal_id", "ix_runs_principal_active", "ix_runs_tenant_active"):
        op.drop_index(index, table_name="runs")
    op.drop_constraint("ck_runs_work_kind", "runs", type_="check")
    op.drop_column("runs", "work_kind")
    op.drop_column("runs", "principal_id")
