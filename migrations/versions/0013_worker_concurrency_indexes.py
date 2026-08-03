"""Add per-ticket indexes for concurrent worker claims."""

from alembic import op

revision = "0013_worker_concurrency_indexes"
down_revision = "0012_web_support"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_notification_outbox_ticket_status_created",
        "notification_outbox",
        ["ticket_id", "status", "created_at"],
    )
    op.create_index(
        "ix_reconciliation_ticket_status_created",
        "reconciliation_outbox",
        ["ticket_id", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_reconciliation_ticket_status_created",
        table_name="reconciliation_outbox",
    )
    op.drop_index(
        "ix_notification_outbox_ticket_status_created",
        table_name="notification_outbox",
    )
