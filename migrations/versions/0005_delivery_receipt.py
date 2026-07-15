"""Store Telegram delivery receipts for correlation and diagnostics."""

import sqlalchemy as sa
from alembic import op

revision = "0005_delivery_receipt"
down_revision = "0004_query_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "delivery_outbox",
        sa.Column("delivered_message_id", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("delivery_outbox", "delivered_message_id")
