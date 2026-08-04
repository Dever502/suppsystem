"""Add durable quick replies."""

import sqlalchemy as sa
from alembic import op

revision = "0015_quick_replies"
down_revision = "0014_api_ingress_ordering"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quick_replies",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("title", sa.String(64), nullable=False),
        sa.Column("normalized_title", sa.String(256), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_by_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("created_by_display_name", sa.String(255)),
        sa.Column("created_by_username", sa.String(255)),
        sa.Column("source_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("source_message_id", sa.BigInteger(), nullable=False),
        sa.Column("published_message_id", sa.BigInteger()),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "normalized_title",
            name="uq_quick_replies_normalized_title",
        ),
        sa.UniqueConstraint(
            "published_message_id",
            name="uq_quick_replies_published_message",
        ),
        sa.UniqueConstraint(
            "source_chat_id",
            "source_message_id",
            name="uq_quick_replies_source",
        ),
    )
    op.create_index(
        "ix_quick_replies_active_id",
        "quick_replies",
        ["active", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_quick_replies_active_id", table_name="quick_replies")
    op.drop_table("quick_replies")
