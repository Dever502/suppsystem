"""Create the MVP support schema."""

import sqlalchemy as sa
from alembic import op

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("display_name", sa.String(length=255)),
        sa.Column("username", sa.String(length=255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "user_identities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", "external_id", name="uq_identity_provider_external"),
    )
    op.create_table(
        "tickets",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("topic_id", sa.BigInteger(), unique=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("topic_provisioning_token", sa.String(length=36), unique=True),
        sa.Column("topic_provisioning_started_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "ticket_messages",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "ticket_id",
            sa.String(length=36),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("direction", sa.String(length=32), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("source_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("source_message_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "direction", "source_chat_id", "source_message_id", name="uq_ticket_message_source"
        ),
    )
    op.create_table(
        "delivery_outbox",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "ticket_id",
            sa.String(length=36),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False, unique=True),
        sa.Column("direction", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "operator_actions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "ticket_id", sa.String(length=36), sa.ForeignKey("tickets.id", ondelete="SET NULL")
        ),
        sa.Column("operator_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False, unique=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("result", sa.String(length=64)),
        sa.Column("trace_id", sa.String(length=64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "blocklist",
        sa.Column("telegram_user_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("blocked_by_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    for table in (
        "blocklist",
        "operator_actions",
        "delivery_outbox",
        "ticket_messages",
        "tickets",
        "user_identities",
        "users",
    ):
        op.drop_table(table)
