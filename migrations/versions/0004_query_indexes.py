"""Add indexes for delivery claims, ticket history, and unresolved actions."""

import sqlalchemy as sa
from alembic import op

revision = "0004_query_indexes"
down_revision = "0003_notification_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_tickets_status_updated",
        "tickets",
        ["status", "updated_at"],
    )
    op.create_index(
        "ix_ticket_messages_ticket_created",
        "ticket_messages",
        ["ticket_id", "created_at"],
    )
    op.create_index(
        "ix_delivery_outbox_claim",
        "delivery_outbox",
        ["status", "next_attempt_at", "created_at"],
    )
    op.create_index(
        "ix_delivery_outbox_stale",
        "delivery_outbox",
        ["status", "claimed_at"],
    )
    op.create_index(
        "ix_delivery_outbox_ticket_direction_status",
        "delivery_outbox",
        ["ticket_id", "direction", "status"],
    )
    op.create_index(
        "ix_delivery_outbox_ticket_status_created",
        "delivery_outbox",
        ["ticket_id", "status", "created_at"],
    )
    op.create_index(
        "ix_notification_outbox_claim",
        "notification_outbox",
        ["status", "next_attempt_at", "created_at"],
    )
    op.create_index(
        "ix_notification_outbox_stale",
        "notification_outbox",
        ["status", "claimed_at"],
    )
    op.create_index(
        "ix_operator_actions_ticket_action_result",
        "operator_actions",
        ["ticket_id", "action", "result"],
    )
    op.create_index(
        "ix_operator_actions_result_created",
        "operator_actions",
        ["result", "created_at"],
    )
    op.create_index(
        "uq_operator_actions_unresolved_ticket",
        "operator_actions",
        ["ticket_id"],
        unique=True,
        sqlite_where=sa.text("result IN ('started', 'unknown') AND action LIKE 'remnawave_%'"),
        postgresql_where=sa.text("result IN ('started', 'unknown') AND action LIKE 'remnawave_%'"),
    )


def downgrade() -> None:
    for table_name, index_name in (
        ("operator_actions", "uq_operator_actions_unresolved_ticket"),
        ("operator_actions", "ix_operator_actions_result_created"),
        ("operator_actions", "ix_operator_actions_ticket_action_result"),
        ("notification_outbox", "ix_notification_outbox_stale"),
        ("notification_outbox", "ix_notification_outbox_claim"),
        ("delivery_outbox", "ix_delivery_outbox_ticket_status_created"),
        ("delivery_outbox", "ix_delivery_outbox_ticket_direction_status"),
        ("delivery_outbox", "ix_delivery_outbox_stale"),
        ("delivery_outbox", "ix_delivery_outbox_claim"),
        ("ticket_messages", "ix_ticket_messages_ticket_created"),
        ("tickets", "ix_tickets_status_updated"),
    ):
        op.drop_index(index_name, table_name=table_name)
