"""Replace the grouped quick reply catalog with flat topic messages."""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0017_flat_quick_responses"
down_revision = "0016_quick_reply_groups"
branch_labels = None
depends_on = None


def _quick_responses_table() -> sa.sql.expression.TableClause:
    return sa.table(
        "quick_responses",
        sa.column("id", sa.Integer()),
        sa.column("text", sa.Text()),
        sa.column("tags", sa.JSON()),
        sa.column("created_by_telegram_id", sa.BigInteger()),
        sa.column("created_by_display_name", sa.String(255)),
        sa.column("created_by_username", sa.String(255)),
        sa.column("source_chat_id", sa.BigInteger()),
        sa.column("source_message_id", sa.BigInteger()),
        sa.column("published_message_id", sa.BigInteger()),
        sa.column("state", sa.String(24)),
        sa.column("invalid_until", sa.DateTime(timezone=True)),
        sa.column("warning_message_id", sa.BigInteger()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )


def upgrade() -> None:
    op.create_table(
        "quick_responses",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("created_by_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("created_by_display_name", sa.String(255)),
        sa.Column("created_by_username", sa.String(255)),
        sa.Column("source_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("source_message_id", sa.BigInteger(), nullable=False),
        sa.Column("published_message_id", sa.BigInteger()),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("invalid_until", sa.DateTime(timezone=True)),
        sa.Column("warning_message_id", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "published_message_id",
            name="uq_quick_responses_published_message",
        ),
        sa.UniqueConstraint(
            "source_chat_id",
            "source_message_id",
            name="uq_quick_responses_source",
        ),
    )
    op.create_index(
        "ix_quick_responses_state_deadline",
        "quick_responses",
        ["state", "invalid_until", "id"],
    )

    connection = op.get_bind()
    metadata = sa.MetaData()
    old_replies = sa.Table("quick_replies", metadata, autoload_with=connection)
    old_groups = sa.Table("quick_reply_groups", metadata, autoload_with=connection)
    active_replies = connection.execute(
        sa.select(old_replies).where(old_replies.c.active.is_(True)).order_by(old_replies.c.id)
    ).mappings()
    response_rows = [
        {
            "text": row["text"],
            "tags": [],
            "created_by_telegram_id": row["created_by_telegram_id"],
            "created_by_display_name": row["created_by_display_name"],
            "created_by_username": row["created_by_username"],
            "source_chat_id": row["source_chat_id"],
            "source_message_id": row["source_message_id"],
            # Legacy cards are replaced with plain messages at startup.
            "published_message_id": None,
            "state": "valid",
            "invalid_until": None,
            "warning_message_id": None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in active_replies
    ]
    if response_rows:
        op.bulk_insert(_quick_responses_table(), response_rows)

    settings = sa.Table("system_settings", metadata, autoload_with=connection)
    now = datetime.now(UTC)
    legacy_publications = list(
        connection.execute(
            sa.select(old_groups.c.published_message_id).where(
                old_groups.c.published_message_id.is_not(None)
            )
        ).scalars()
    ) + list(
        connection.execute(
            sa.select(old_replies.c.published_message_id).where(
                old_replies.c.published_message_id.is_not(None)
            )
        ).scalars()
    )
    for sequence, message_id in enumerate(legacy_publications):
        connection.execute(
            settings.insert().values(
                key=f"telegram_quick_reply_legacy:{sequence}",
                value=str(message_id),
                updated_at=now,
            )
        )

    op.drop_table("quick_replies")
    op.drop_table("quick_reply_groups")


def downgrade() -> None:
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

    now = datetime.now(UTC)
    groups = sa.table(
        "quick_reply_groups",
        sa.column("id", sa.Integer()),
        sa.column("name", sa.String(64)),
        sa.column("normalized_name", sa.String(256)),
        sa.column("created_by_telegram_id", sa.BigInteger()),
        sa.column("source_chat_id", sa.BigInteger()),
        sa.column("source_message_id", sa.BigInteger()),
        sa.column("active", sa.Boolean()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(
        groups,
        [
            {
                "name": "Общее",
                "normalized_name": "общее",
                "created_by_telegram_id": 0,
                "source_chat_id": 0,
                "source_message_id": 0,
                "active": True,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )

    op.create_table(
        "quick_replies",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["quick_reply_groups.id"],
            name="fk_quick_replies_group_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "group_id",
            "normalized_title",
            name="uq_quick_replies_group_title",
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
        "ix_quick_replies_group_active_id",
        "quick_replies",
        ["group_id", "active", "id"],
    )

    connection = op.get_bind()
    metadata = sa.MetaData()
    responses = sa.Table("quick_responses", metadata, autoload_with=connection)
    replies = sa.Table("quick_replies", metadata, autoload_with=connection)
    rows = connection.execute(
        sa.select(responses).where(responses.c.state == "valid").order_by(responses.c.id)
    ).mappings()
    for row in rows:
        title = f"Быстрый ответ {row['id']}"
        connection.execute(
            replies.insert().values(
                group_id=1,
                title=title,
                normalized_title=title.casefold(),
                text=row["text"],
                created_by_telegram_id=row["created_by_telegram_id"],
                created_by_display_name=row["created_by_display_name"],
                created_by_username=row["created_by_username"],
                source_chat_id=row["source_chat_id"],
                source_message_id=row["source_message_id"],
                published_message_id=row["published_message_id"],
                active=True,
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        )

    op.drop_table("quick_responses")
