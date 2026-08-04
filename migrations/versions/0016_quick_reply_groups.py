"""Add groups to the quick reply catalog."""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0016_quick_reply_groups"
down_revision = "0015_quick_replies"
branch_labels = None
depends_on = None

_DEFAULT_GROUP_NAME = "Общее"
_DEFAULT_NORMALIZED_NAME = "общее"


def upgrade() -> None:
    op.create_table(
        "quick_reply_groups",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("normalized_name", sa.String(256), nullable=False),
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
            "normalized_name",
            name="uq_quick_reply_groups_normalized_name",
        ),
        sa.UniqueConstraint(
            "published_message_id",
            name="uq_quick_reply_groups_published_message",
        ),
        sa.UniqueConstraint(
            "source_chat_id",
            "source_message_id",
            name="uq_quick_reply_groups_source",
        ),
    )
    op.create_index(
        "ix_quick_reply_groups_active_id",
        "quick_reply_groups",
        ["active", "id"],
    )

    groups = sa.table(
        "quick_reply_groups",
        sa.column("name", sa.String(64)),
        sa.column("normalized_name", sa.String(256)),
        sa.column("created_by_telegram_id", sa.BigInteger()),
        sa.column("source_chat_id", sa.BigInteger()),
        sa.column("source_message_id", sa.BigInteger()),
        sa.column("active", sa.Boolean()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    now = datetime.now(UTC)
    op.bulk_insert(
        groups,
        [
            {
                "name": _DEFAULT_GROUP_NAME,
                "normalized_name": _DEFAULT_NORMALIZED_NAME,
                "created_by_telegram_id": 0,
                "source_chat_id": 0,
                "source_message_id": 0,
                "active": True,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )

    with op.batch_alter_table("quick_replies") as batch:
        batch.add_column(sa.Column("group_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_quick_replies_group_id",
            "quick_reply_groups",
            ["group_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    op.execute(
        sa.text(
            "UPDATE quick_replies "
            "SET group_id = ("
            "SELECT id FROM quick_reply_groups WHERE normalized_name = :normalized_name"
            ") WHERE group_id IS NULL"
        ).bindparams(normalized_name=_DEFAULT_NORMALIZED_NAME)
    )

    with op.batch_alter_table("quick_replies") as batch:
        batch.drop_constraint(
            "uq_quick_replies_normalized_title",
            type_="unique",
        )
        batch.alter_column(
            "group_id",
            existing_type=sa.Integer(),
            nullable=False,
        )
        batch.create_unique_constraint(
            "uq_quick_replies_group_title",
            ["group_id", "normalized_title"],
        )
        batch.drop_index("ix_quick_replies_active_id")
        batch.create_index(
            "ix_quick_replies_group_active_id",
            ["group_id", "active", "id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("quick_replies") as batch:
        batch.drop_index("ix_quick_replies_group_active_id")
        batch.create_index(
            "ix_quick_replies_active_id",
            ["active", "id"],
        )
        batch.drop_constraint(
            "uq_quick_replies_group_title",
            type_="unique",
        )
        batch.create_unique_constraint(
            "uq_quick_replies_normalized_title",
            ["normalized_title"],
        )
        batch.drop_constraint(
            "fk_quick_replies_group_id",
            type_="foreignkey",
        )
        batch.drop_column("group_id")

    op.drop_index(
        "ix_quick_reply_groups_active_id",
        table_name="quick_reply_groups",
    )
    op.drop_table("quick_reply_groups")
