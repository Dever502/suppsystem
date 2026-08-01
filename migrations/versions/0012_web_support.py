"""Add Web support identities, media, lifecycle events, and dashboard state."""

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0012_web_support"
down_revision = "0011_durable_ingress_reconciliation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("email", sa.String(320)))

    with op.batch_alter_table("tickets") as batch_op:
        batch_op.add_column(
            sa.Column("channel", sa.String(32), server_default="telegram", nullable=False)
        )
        batch_op.add_column(sa.Column("remnawave_user_uuid", sa.String(36)))

    bind = op.get_bind()
    user_unique = next(
        constraint
        for constraint in sa.inspect(bind).get_unique_constraints("tickets")
        if constraint.get("column_names") == ["user_id"]
    )
    reflected_name = user_unique.get("name")
    naming_convention = {"uq": "uq_%(table_name)s_%(column_0_name)s"}
    with op.batch_alter_table(
        "tickets",
        naming_convention=naming_convention if reflected_name is None else None,
    ) as batch_op:
        batch_op.drop_constraint(
            str(reflected_name or "uq_tickets_user_id"),
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_ticket_user_channel",
            ["user_id", "channel"],
        )

    with op.batch_alter_table("ticket_messages") as batch_op:
        batch_op.add_column(
            sa.Column("suppressed", sa.Boolean(), server_default=sa.false(), nullable=False)
        )

    op.create_table(
        "support_blocks",
        sa.Column("ticket_id", sa.String(36), nullable=False),
        sa.Column("blocked_by_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("source", sa.String(32), server_default="telegram", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("ticket_id"),
    )

    bind.execute(
        sa.text(
            """
            INSERT INTO support_blocks (
                ticket_id, blocked_by_telegram_id, reason, source, created_at
            )
            SELECT t.id, b.blocked_by_telegram_id, b.reason, 'migration', b.created_at
            FROM blocklist AS b
            JOIN user_identities AS ui
              ON ui.provider = 'telegram'
             AND ui.external_id = CAST(b.telegram_user_id AS VARCHAR)
            JOIN tickets AS t
              ON t.user_id = ui.user_id
             AND t.channel = 'telegram'
            """
        )
    )

    op.create_index(
        "ix_ticket_messages_direction_channel_created",
        "ticket_messages",
        ["direction", "channel", "created_at"],
    )

    op.create_table(
        "media_assets",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("ticket_id", sa.String(36), nullable=False),
        sa.Column("message_id", sa.String(36), nullable=False),
        sa.Column("storage_path", sa.String(512), nullable=False),
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("original_filename", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["ticket_messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id"),
        sa.UniqueConstraint("storage_path", name="uq_media_asset_storage_path"),
    )
    op.create_index("ix_media_assets_ticket_created", "media_assets", ["ticket_id", "created_at"])

    op.create_table(
        "ticket_lifecycle_events",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("ticket_id", sa.String(36), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("close_cycle", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ticket_lifecycle_event_time",
        "ticket_lifecycle_events",
        ["event_type", "created_at"],
    )
    op.create_index(
        "ix_ticket_lifecycle_ticket_time",
        "ticket_lifecycle_events",
        ["ticket_id", "created_at"],
    )

    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("value", sa.String(1000), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "operator_dashboard_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.BigInteger()),
        sa.Column("period", sa.String(16), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    tickets = sa.table(
        "tickets",
        sa.column("id", sa.String(36)),
        sa.column("channel", sa.String(32)),
        sa.column("close_cycle", sa.Integer()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    lifecycle = sa.table(
        "ticket_lifecycle_events",
        sa.column("id", sa.String(36)),
        sa.column("ticket_id", sa.String(36)),
        sa.column("event_type", sa.String(32)),
        sa.column("channel", sa.String(32)),
        sa.column("close_cycle", sa.Integer()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    rows = bind.execute(
        sa.select(tickets.c.id, tickets.c.channel, tickets.c.close_cycle, tickets.c.created_at)
    )
    for row in rows:
        bind.execute(
            lifecycle.insert().values(
                id=str(uuid.uuid4()),
                ticket_id=row.id,
                event_type="created",
                channel=row.channel,
                close_cycle=row.close_cycle,
                created_at=row.created_at,
            )
        )


def downgrade() -> None:
    op.drop_table("operator_dashboard_state")
    op.drop_table("system_settings")
    op.drop_index("ix_ticket_lifecycle_ticket_time", table_name="ticket_lifecycle_events")
    op.drop_index("ix_ticket_lifecycle_event_time", table_name="ticket_lifecycle_events")
    op.drop_table("ticket_lifecycle_events")
    op.drop_index("ix_media_assets_ticket_created", table_name="media_assets")
    op.drop_table("media_assets")
    op.drop_table("support_blocks")
    with op.batch_alter_table("ticket_messages") as batch_op:
        batch_op.drop_column("suppressed")
    op.drop_index(
        "ix_ticket_messages_direction_channel_created",
        table_name="ticket_messages",
    )
    with op.batch_alter_table("tickets") as batch_op:
        batch_op.drop_constraint("uq_ticket_user_channel", type_="unique")
        batch_op.create_unique_constraint("uq_tickets_user_id", ["user_id"])
        batch_op.drop_column("remnawave_user_uuid")
        batch_op.drop_column("channel")
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("email")
