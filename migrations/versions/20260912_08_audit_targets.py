"""Sensitive read audit targets; no FK so denied/missing targets can be audited."""

import sqlalchemy as sa
from alembic import op

revision = "20260912_08"
down_revision = "20260912_07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("security_audit_events", sa.Column("principal_id", sa.String(128), nullable=True))
    op.add_column("security_audit_events", sa.Column("target_run_id", sa.Uuid(), nullable=True))
    op.add_column("security_audit_events", sa.Column("resource", sa.String(16), nullable=True))


def downgrade() -> None:
    for column in ("resource", "target_run_id", "principal_id"):
        op.drop_column("security_audit_events", column)
