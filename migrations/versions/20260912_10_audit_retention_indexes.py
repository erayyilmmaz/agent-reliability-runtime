"""Index security_audit_events for the two queries it exists to serve.

PERF-005. The table carried indexes only on its primary key and client_id, so
both of its real access patterns were sequential scans:

  forensics  WHERE target_run_id = ? ORDER BY created_at DESC
  retention  WHERE created_at < ?

At 25k rows that measured 4.58 ms and 511 shared buffers, growing linearly with
a table whose size is driven by read traffic.

Created CONCURRENTLY: this table is written on the request path, and a plain
CREATE INDEX takes a lock that would block every authenticated request for the
duration of the build.
"""

from alembic import op

revision = "20260912_10"
down_revision = "20260912_09"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction block.
    with op.get_context().autocommit_block():
        # Partial: only sensitive-read rows carry a target, so this stays much
        # smaller than the table. The trailing created_at also serves the
        # ORDER BY, removing the top-N heapsort the forensic query paid for.
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
                ix_security_audit_events_target
            ON security_audit_events (target_run_id, created_at DESC)
            WHERE target_run_id IS NOT NULL
            """
        )
        # Drives the retention scan, and keeps it an index-only scan.
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
                ix_security_audit_events_created_at
            ON security_audit_events (created_at)
            """
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_security_audit_events_created_at")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_security_audit_events_target")
