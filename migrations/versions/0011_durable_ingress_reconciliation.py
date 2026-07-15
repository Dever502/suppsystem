"""Add durable ingress, reconciliation work, and notification claim ownership."""

import sqlalchemy as sa
from alembic import op

revision = "0011_durable_ingress_reconciliation"
down_revision = "0010_delivery_claim_ownership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        # Alembic updates version_num after this function returns. The current
        # revision is 35 characters long, while Alembic creates this column as
        # VARCHAR(32) by default, so PostgreSQL needs the column widened first.
        op.alter_column(
            "alembic_version",
            "version_num",
            existing_type=sa.String(32),
            type_=sa.String(64),
            existing_nullable=False,
        )
    op.add_column("notification_outbox", sa.Column("claim_token", sa.String(36)))
    notifications = sa.table(
        "notification_outbox",
        sa.column("status", sa.String()),
        sa.column("claimed_at", sa.DateTime(timezone=True)),
        sa.column("claim_token", sa.String(36)),
    )
    op.execute(
        sa.update(notifications)
        .where(notifications.c.status == "processing")
        .values(status="pending", claimed_at=None, claim_token=None)
    )
    op.create_table(
        "inbound_updates",
        sa.Column("telegram_update_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("claim_token", sa.String(36)),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("telegram_update_id"),
    )
    op.create_index(
        "ix_inbound_updates_claim",
        "inbound_updates",
        ["status", "next_attempt_at", "telegram_update_id"],
    )
    op.create_index("ix_inbound_updates_stale", "inbound_updates", ["status", "claimed_at"])
    op.create_table(
        "reconciliation_outbox",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("ticket_id", sa.String(36)),
        sa.Column("operator_action_id", sa.String(36)),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("claim_token", sa.String(36)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["operator_action_id"], ["operator_actions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.UniqueConstraint("operator_action_id"),
    )
    op.create_index(
        "ix_reconciliation_claim",
        "reconciliation_outbox",
        ["status", "next_attempt_at", "created_at"],
    )
    op.create_index("ix_reconciliation_stale", "reconciliation_outbox", ["status", "claimed_at"])


def downgrade() -> None:
    op.drop_index("ix_reconciliation_stale", table_name="reconciliation_outbox")
    op.drop_index("ix_reconciliation_claim", table_name="reconciliation_outbox")
    op.drop_table("reconciliation_outbox")
    op.drop_index("ix_inbound_updates_stale", table_name="inbound_updates")
    op.drop_index("ix_inbound_updates_claim", table_name="inbound_updates")
    op.drop_table("inbound_updates")
    op.drop_column("notification_outbox", "claim_token")
