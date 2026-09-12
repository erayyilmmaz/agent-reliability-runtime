"""Give the append-only audit trigger an explicit, non-blocking purge gate.

PERF-005. ``security_audit_events`` grows with read traffic and nothing ever
removes a row, so the table is unbounded by construction. Retention needs a way
to delete aged rows without weakening the append-only guarantee.

The obvious mechanism -- ``ALTER TABLE ... DISABLE TRIGGER`` around the purge --
was measured and rejected. It takes ``ShareRowExclusiveLock``, which conflicts
with the ``RowExclusiveLock`` an INSERT needs: during a local test an audit
INSERT issued while the purge transaction held that lock stalled for the full
transaction duration (4 s against a 6 s statement). Audit writes sit on the
request path, so that is an API stall. It is also the wrong blast radius --
while the trigger is off, *every* session's deletes pass, not just the purge's.

This replaces the flag with a transaction-scoped one. The trigger still refuses
every UPDATE and every DELETE, unless the deleting transaction has explicitly
set ``arr.allow_audit_purge = 'on'``. ``SET LOCAL`` scopes that to the one
transaction, it takes no table-level lock, and it leaves concurrent writers
untouched.

The GUC is not the security boundary and is not meant to be one. Per SEC-018
the runtime role holds INSERT only on this table, so it cannot delete whatever
it sets; the boundary is the grant. What the gate buys is that the owner role
cannot delete audit history *by accident* -- only by saying so in the same
transaction.
"""

from alembic import op

revision = "20260912_11"
down_revision = "20260912_10"
branch_labels = None
depends_on = None

PURGE_GUC = "arr.allow_audit_purge"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION prevent_security_audit_mutation() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE'
               AND current_setting('{PURGE_GUC}', true) = 'on' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'security_audit_events are append-only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_security_audit_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'security_audit_events are append-only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
