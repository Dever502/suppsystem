"""Create notification outbox for cross-channel user notifications."""

import sqlalchemy as sa
from alembic import op

revision = "0003_notification_outbox"
down_revision = "0002_ticket_message_content"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "ticket_id",
            sa.String(length=36),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False, unique=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("destination", sa.String(length=64), nullable=False),
        sa.Column("recipient_identity_provider", sa.String(length=32), nullable=False),
        sa.Column("recipient_identity_value", sa.String(length=255), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("notification_outbox")
