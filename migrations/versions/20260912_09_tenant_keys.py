"""Mutable tenant DEK registry for the opt-in envelope-encryption adapter."""

import sqlalchemy as sa
from alembic import op

revision = "20260912_09"
down_revision = "20260912_08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_keys",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("last_encrypted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("destroyed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("tenant_keys")
