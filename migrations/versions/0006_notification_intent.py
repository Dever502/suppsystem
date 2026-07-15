"""Link notification intents to the operator action that created them."""

import sqlalchemy as sa
from alembic import op

revision = "0006_notification_intent"
down_revision = "0005_delivery_receipt"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("notification_outbox") as batch_op:
        batch_op.add_column(sa.Column("operator_action_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            "fk_notification_outbox_operator_action",
            "operator_actions",
            ["operator_action_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_unique_constraint(
            "uq_notification_outbox_operator_action",
            ["operator_action_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("notification_outbox") as batch_op:
        batch_op.drop_constraint(
            "uq_notification_outbox_operator_action",
            type_="unique",
        )
        batch_op.drop_constraint(
            "fk_notification_outbox_operator_action",
            type_="foreignkey",
        )
        batch_op.drop_column("operator_action_id")
