"""Publish each valid quick response as one canonical bot message."""

import sqlalchemy as sa
from alembic import op

revision = "0019_canonical_quick_responses"
down_revision = "0018_quick_response_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "quick_responses",
        "status_message_id",
        new_column_name="warning_message_id",
        existing_type=sa.BigInteger(),
        existing_nullable=True,
    )
    op.add_column(
        "quick_responses",
        sa.Column(
            "publication_format_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    quick_responses = sa.table(
        "quick_responses",
        sa.column("published_message_id", sa.BigInteger()),
        sa.column("state", sa.String(24)),
    )
    op.execute(
        quick_responses.update()
        .where(quick_responses.c.state == "pending_deletion")
        .values(published_message_id=None)
    )


def downgrade() -> None:
    op.drop_column("quick_responses", "publication_format_version")
    op.alter_column(
        "quick_responses",
        "warning_message_id",
        new_column_name="status_message_id",
        existing_type=sa.BigInteger(),
        existing_nullable=True,
    )
