"""Add a completion timestamp to evaluation lifecycle records."""

import sqlalchemy as sa
from alembic import op

revision = "20260906_05"
down_revision = "20260906_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "evaluations", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("evaluations", "completed_at")
