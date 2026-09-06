"""Add durable retry scheduling to runs.

Revision ID: 20260906_03
Revises: 20260906_02
Create Date: 2026-09-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260906_03"
down_revision: str | None = "20260906_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_runs_next_attempt_at", "runs", ["next_attempt_at"])


def downgrade() -> None:
    op.drop_index("ix_runs_next_attempt_at", table_name="runs")
    op.drop_column("runs", "next_attempt_at")
