"""Track the single status reply attached to each quick response."""

import sqlalchemy as sa
from alembic import op

revision = "0018_quick_response_status"
down_revision = "0017_flat_quick_responses"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "quick_responses",
        "warning_message_id",
        new_column_name="status_message_id",
        existing_type=sa.BigInteger(),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "quick_responses",
        "status_message_id",
        new_column_name="warning_message_id",
        existing_type=sa.BigInteger(),
        existing_nullable=True,
    )
