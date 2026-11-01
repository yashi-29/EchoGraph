"""add sync_state to connectors

Revision ID: d5f1a8c3e927
Revises: a7d3e9b2c418
Create Date: 2026-10-07 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5f1a8c3e927"
down_revision: str | None = "a7d3e9b2c418"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "connectors",
        sa.Column(
            "sync_state",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("connectors", "sync_state")
