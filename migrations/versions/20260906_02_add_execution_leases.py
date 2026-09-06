"""Add execution lease ownership and worker identity.

Revision ID: 20260906_02
Revises: 20260906_01
Create Date: 2026-09-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260906_02"
down_revision: str | None = "20260906_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("execution_lease_owner", sa.String(length=128), nullable=True))
    op.add_column(
        "runs", sa.Column("execution_lease_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index("ix_runs_execution_lease_expires_at", "runs", ["execution_lease_expires_at"])
    op.add_column("run_attempts", sa.Column("worker_id", sa.String(length=128), nullable=True))
    op.execute("UPDATE run_attempts SET worker_id = 'legacy' WHERE worker_id IS NULL")
    op.alter_column("run_attempts", "worker_id", nullable=False)


def downgrade() -> None:
    op.drop_column("run_attempts", "worker_id")
    op.drop_index("ix_runs_execution_lease_expires_at", table_name="runs")
    op.drop_column("runs", "execution_lease_expires_at")
    op.drop_column("runs", "execution_lease_owner")
