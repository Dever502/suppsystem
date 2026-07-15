"""Track and index ticket activity independently from row updates."""

import sqlalchemy as sa
from alembic import op

revision = "0009_ticket_last_activity"
down_revision = "0008_rating_cycles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tickets") as batch_op:
        batch_op.add_column(sa.Column("last_activity_at", sa.DateTime(timezone=True)))

    tickets = sa.table(
        "tickets",
        sa.column("id", sa.String()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("last_activity_at", sa.DateTime(timezone=True)),
    )
    messages = sa.table(
        "ticket_messages",
        sa.column("ticket_id", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    latest_message = (
        sa.select(sa.func.max(messages.c.created_at))
        .where(messages.c.ticket_id == tickets.c.id)
        .scalar_subquery()
    )
    op.execute(
        sa.update(tickets).values(
            last_activity_at=sa.case(
                (latest_message > tickets.c.updated_at, latest_message),
                else_=tickets.c.updated_at,
            )
        )
    )

    with op.batch_alter_table("tickets") as batch_op:
        batch_op.alter_column("last_activity_at", nullable=False)
        batch_op.create_index(
            "ix_tickets_status_last_activity",
            ["status", "last_activity_at"],
        )


def downgrade() -> None:
    with op.batch_alter_table("tickets") as batch_op:
        batch_op.drop_index("ix_tickets_status_last_activity")
        batch_op.drop_column("last_activity_at")
