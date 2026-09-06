"""Create append-only security audit records for API boundary decisions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260906_06"
down_revision = "20260906_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "security_audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("client_id", sa.String(length=128), nullable=True),
        sa.Column("credential_fingerprint", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_security_audit_events_client_id", "security_audit_events", ["client_id"])
    op.execute(
        """
        CREATE FUNCTION prevent_security_audit_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'security_audit_events are append-only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER security_audit_events_no_update_or_delete BEFORE UPDATE OR DELETE "
        "ON security_audit_events FOR EACH ROW EXECUTE FUNCTION prevent_security_audit_mutation();"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER security_audit_events_no_update_or_delete ON security_audit_events")
    op.execute("DROP FUNCTION prevent_security_audit_mutation")
    op.drop_index("ix_security_audit_events_client_id", table_name="security_audit_events")
    op.drop_table("security_audit_events")
