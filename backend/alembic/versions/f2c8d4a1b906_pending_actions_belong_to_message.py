"""link pending_actions to their chat message (one message can propose several actions)

Replaces conversation_messages.pending_action_id (at most one action per reply) with
pending_actions.message_id, so a single turn like "add Neha's birthday and email her"
can produce a calendar draft *and* an email draft.

Revision ID: f2c8d4a1b906
Revises: e3a9f1c4b702
Create Date: 2026-09-25 20:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2c8d4a1b906"
down_revision: str | None = "e3a9f1c4b702"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("pending_actions", sa.Column("message_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "pending_actions_message_id_fkey",
        "pending_actions",
        "conversation_messages",
        ["message_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_pending_actions_message_id", "pending_actions", ["message_id"])
    op.execute(
        "UPDATE pending_actions SET message_id = m.id FROM conversation_messages m "
        "WHERE m.pending_action_id = pending_actions.id"
    )
    op.drop_column("conversation_messages", "pending_action_id")


def downgrade() -> None:
    op.add_column("conversation_messages", sa.Column("pending_action_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "conversation_messages_pending_action_id_fkey",
        "conversation_messages",
        "pending_actions",
        ["pending_action_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Only one action per message fits the old shape — keep the earliest.
    op.execute(
        "UPDATE conversation_messages m SET pending_action_id = a.id FROM ("
        "  SELECT DISTINCT ON (message_id) id, message_id FROM pending_actions"
        "  WHERE message_id IS NOT NULL ORDER BY message_id, created_at"
        ") a WHERE a.message_id = m.id"
    )
    op.drop_index("ix_pending_actions_message_id", table_name="pending_actions")
    op.drop_constraint("pending_actions_message_id_fkey", "pending_actions", type_="foreignkey")
    op.drop_column("pending_actions", "message_id")
