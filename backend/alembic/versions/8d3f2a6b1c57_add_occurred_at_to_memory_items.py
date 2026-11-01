"""add occurred_at to memory_items

Revision ID: 8d3f2a6b1c57
Revises: 5b1e7c9d2a40
Create Date: 2026-09-24 23:00:00.000000

"""

from collections.abc import Sequence
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8d3f2a6b1c57"
down_revision: str | None = "5b1e7c9d2a40"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


# Parsing is inlined (not imported from app.services) so this migration keeps doing the
# same thing even if the ingestion helpers change later.
def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _backfill_date(source: str, metadata: dict) -> datetime | None:
    try:
        if source == "gmail" and metadata.get("date"):
            return _aware(parsedate_to_datetime(metadata["date"]))
        if source == "google_calendar" and metadata.get("start"):
            start = metadata["start"]
            if "T" not in start:
                day = date.fromisoformat(start)
                return datetime(day.year, day.month, day.day, tzinfo=UTC)
            return _aware(datetime.fromisoformat(start.replace("Z", "+00:00")))
    except (TypeError, ValueError, IndexError):
        return None
    return None


def upgrade() -> None:
    op.add_column("memory_items", sa.Column("occurred_at", sa.DateTime(timezone=True)))

    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, source, item_metadata FROM memory_items")).all()
    for row_id, source, metadata in rows:
        occurred_at = _backfill_date(source, metadata or {})
        if occurred_at is not None:
            conn.execute(
                sa.text("UPDATE memory_items SET occurred_at = :at WHERE id = :id"),
                {"at": occurred_at, "id": row_id},
            )
    # No usable date in the metadata → fall back to when it was indexed.
    conn.execute(
        sa.text("UPDATE memory_items SET occurred_at = created_at WHERE occurred_at IS NULL")
    )

    op.alter_column("memory_items", "occurred_at", nullable=False, server_default=sa.text("now()"))
    op.create_index(
        "ix_memory_items_user_occurred_at",
        "memory_items",
        ["user_id", "occurred_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_memory_items_user_occurred_at", table_name="memory_items")
    op.drop_column("memory_items", "occurred_at")
