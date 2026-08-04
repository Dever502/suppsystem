"""Align API storage limits and preserve per-conversation Telegram ordering."""

import sqlalchemy as sa
from alembic import op

revision = "0014_api_ingress_ordering"
down_revision = "0013_worker_concurrency_indexes"
branch_labels = None
depends_on = None


def _alter_string_length(
    table_name: str,
    column_name: str,
    *,
    old_length: int,
    new_length: int,
) -> None:
    with op.batch_alter_table(table_name) as batch_op:
        batch_op.alter_column(
            column_name,
            existing_type=sa.String(old_length),
            type_=sa.String(new_length),
            existing_nullable=False,
        )


def upgrade() -> None:
    _alter_string_length("user_identities", "external_id", old_length=255, new_length=320)
    _alter_string_length(
        "notification_outbox",
        "recipient_identity_value",
        old_length=255,
        new_length=320,
    )
    for table_name in (
        "delivery_outbox",
        "notification_outbox",
        "reconciliation_outbox",
        "operator_actions",
    ):
        _alter_string_length(
            table_name,
            "idempotency_key",
            old_length=255,
            new_length=512,
        )

    with op.batch_alter_table("inbound_updates") as batch_op:
        batch_op.add_column(
            sa.Column(
                "ordering_key",
                sa.String(64),
                server_default="legacy:global",
                nullable=False,
            )
        )
    op.create_index(
        "ix_inbound_updates_ordering",
        "inbound_updates",
        ["ordering_key", "status", "telegram_update_id"],
    )
    with op.batch_alter_table("ticket_messages") as batch_op:
        batch_op.add_column(
            sa.Column(
                "sensitive",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
            )
        )
    op.execute(
        sa.text(
            "UPDATE ticket_messages SET sensitive = true "
            "WHERE channel = 'system' "
            "AND content LIKE '%Ссылка подписки обновлена%'"
        )
    )
    op.create_index(
        "ix_ticket_messages_sensitive_created",
        "ticket_messages",
        ["sensitive", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_ticket_messages_sensitive_created", table_name="ticket_messages")
    with op.batch_alter_table("ticket_messages") as batch_op:
        batch_op.drop_column("sensitive")
    op.drop_index("ix_inbound_updates_ordering", table_name="inbound_updates")
    with op.batch_alter_table("inbound_updates") as batch_op:
        batch_op.drop_column("ordering_key")

    for table_name in (
        "operator_actions",
        "reconciliation_outbox",
        "notification_outbox",
        "delivery_outbox",
    ):
        _alter_string_length(
            table_name,
            "idempotency_key",
            old_length=512,
            new_length=255,
        )
    _alter_string_length(
        "notification_outbox",
        "recipient_identity_value",
        old_length=320,
        new_length=255,
    )
    _alter_string_length("user_identities", "external_id", old_length=320, new_length=255)
