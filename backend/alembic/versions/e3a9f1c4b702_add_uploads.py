"""add uploads table and memory_items.upload_id

Revision ID: e3a9f1c4b702
Revises: b4e9c2d7f813
Create Date: 2026-09-24 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e3a9f1c4b702"
down_revision: str | None = "b4e9c2d7f813"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "uploads",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "sha256", name="uq_upload_user_sha256"),
    )
    op.create_index(op.f("ix_uploads_user_id"), "uploads", ["user_id"])

    op.add_column("memory_items", sa.Column("upload_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_memory_items_upload_id",
        "memory_items",
        "uploads",
        ["upload_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(op.f("ix_memory_items_upload_id"), "memory_items", ["upload_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_memory_items_upload_id"), table_name="memory_items")
    op.drop_constraint("fk_memory_items_upload_id", "memory_items", type_="foreignkey")
    op.drop_column("memory_items", "upload_id")
    op.drop_index(op.f("ix_uploads_user_id"), table_name="uploads")
    op.drop_table("uploads")
