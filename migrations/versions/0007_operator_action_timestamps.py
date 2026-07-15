"""Add lifecycle timestamps to operator actions."""

import sqlalchemy as sa
from alembic import op

revision = "0007_operator_action_timestamps"
down_revision = "0006_notification_intent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("operator_actions") as batch_op:
        batch_op.add_column(sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))

    op.execute("UPDATE operator_actions SET updated_at = created_at")
    op.execute(
        "UPDATE operator_actions SET completed_at = created_at "
        "WHERE result IS NOT NULL AND result NOT IN ('started', 'unknown')"
    )

    with op.batch_alter_table("operator_actions") as batch_op:
        batch_op.alter_column("updated_at", nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("operator_actions") as batch_op:
        batch_op.drop_column("completed_at")
        batch_op.drop_column("updated_at")
