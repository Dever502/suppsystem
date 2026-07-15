"""Fence delivery state transitions with durable claim ownership."""

import sqlalchemy as sa
from alembic import op

revision = "0010_delivery_claim_ownership"
down_revision = "0009_ticket_last_activity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "delivery_outbox",
        sa.Column("claim_token", sa.String(length=36), nullable=True),
    )

    delivery_outbox = sa.table(
        "delivery_outbox",
        sa.column("status", sa.String()),
        sa.column("claimed_at", sa.DateTime(timezone=True)),
        sa.column("claim_token", sa.String(length=36)),
    )
    op.execute(
        sa.update(delivery_outbox)
        .where(delivery_outbox.c.status == "processing")
        .values(status="pending", claimed_at=None, claim_token=None)
    )


def downgrade() -> None:
    op.drop_column("delivery_outbox", "claim_token")
