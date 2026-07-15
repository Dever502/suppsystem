"""Bind support ratings to a ticket close cycle."""

import sqlalchemy as sa
from alembic import op

revision = "0008_rating_cycles"
down_revision = "0007_operator_action_timestamps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tickets") as batch_op:
        batch_op.add_column(
            sa.Column("close_cycle", sa.Integer(), server_default="0", nullable=False)
        )

    with op.batch_alter_table("ticket_messages") as batch_op:
        batch_op.add_column(sa.Column("rating_cycle", sa.Integer(), nullable=True))
        batch_op.create_unique_constraint(
            "uq_ticket_message_rating_cycle",
            ["ticket_id", "channel", "rating_cycle"],
        )


def downgrade() -> None:
    with op.batch_alter_table("ticket_messages") as batch_op:
        batch_op.drop_constraint("uq_ticket_message_rating_cycle", type_="unique")
        batch_op.drop_column("rating_cycle")

    with op.batch_alter_table("tickets") as batch_op:
        batch_op.drop_column("close_cycle")
