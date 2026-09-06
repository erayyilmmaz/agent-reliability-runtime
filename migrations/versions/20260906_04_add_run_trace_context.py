"""Persist W3C trace context for retry and fallback propagation.

Revision ID: 20260906_04
Revises: 20260906_03
Create Date: 2026-09-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "20260906_04"
down_revision: str | None = "20260906_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("trace_context", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.alter_column("runs", "trace_context", server_default=None)


def downgrade() -> None:
    op.drop_column("runs", "trace_context")
