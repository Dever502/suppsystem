"""Persist canonical ticket message content and media metadata."""

import sqlalchemy as sa
from alembic import op

revision = "0002_ticket_message_content"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("ticket_messages") as batch:
        batch.alter_column("source_chat_id", existing_type=sa.BigInteger(), nullable=True)
        batch.alter_column("source_message_id", existing_type=sa.BigInteger(), nullable=True)
        batch.add_column(sa.Column("content", sa.Text(), nullable=True))
        batch.add_column(sa.Column("media", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("ticket_messages") as batch:
        batch.drop_column("media")
        batch.drop_column("content")
        batch.alter_column("source_message_id", existing_type=sa.BigInteger(), nullable=False)
        batch.alter_column("source_chat_id", existing_type=sa.BigInteger(), nullable=False)
