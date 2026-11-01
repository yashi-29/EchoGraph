"""add graph_snapshots

Revision ID: a7d3e9b2c418
Revises: f2c8d4a1b906
Create Date: 2026-09-26 00:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7d3e9b2c418"
down_revision: str | None = "f2c8d4a1b906"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "graph_snapshots",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("fingerprint", sa.String(length=128), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("graph_snapshots")
