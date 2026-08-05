"""Keep operator-deleted quick responses as durable tombstones."""

import sqlalchemy as sa
from alembic import op

revision = "0020_quick_response_soft_delete"
down_revision = "0019_canonical_quick_responses"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "quick_responses",
        sa.Column("deleted_by_telegram_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "quick_responses",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("quick_responses", "deleted_at")
    op.drop_column("quick_responses", "deleted_by_telegram_id")
