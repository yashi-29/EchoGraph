"""add profile and preferences to users

Revision ID: b4e9c2d7f813
Revises: 8d3f2a6b1c57
Create Date: 2026-09-24 23:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4e9c2d7f813"
down_revision: str | None = "8d3f2a6b1c57"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("full_name", sa.String(length=100), nullable=True))
    op.add_column(
        "users",
        sa.Column("allow_content_in_chat", sa.Boolean(), server_default="true", nullable=False),
    )
    op.add_column(
        "users",
        sa.Column("response_tone", sa.String(length=20), server_default="balanced", nullable=False),
    )
    op.add_column("users", sa.Column("memory_window_days", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "memory_window_days")
    op.drop_column("users", "response_tone")
    op.drop_column("users", "allow_content_in_chat")
    op.drop_column("users", "full_name")
