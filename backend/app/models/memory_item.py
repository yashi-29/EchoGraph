import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ColumnElement,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Must match Settings.voyage_embedding_dimension (app/core/config.py) — changing either
# without the other requires a migration to alter this column's width.
EMBEDDING_DIM = 1024


class MemoryItem(Base):
    """A single ingested chunk from any connector (email, doc, message, etc).

    Enrichment (LLM-assigned categories/entities) and further chunking of long
    content land on top of this later — see ARCHITECTURE.md's data flow.
    """

    __tablename__ = "memory_items"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "source", "external_id", name="uq_memory_item_source_external_id"
        ),
        # Timeline pagination: newest-first keyset scans over one user's items.
        Index("ix_memory_items_user_occurred_at", "user_id", "occurred_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    # The provider's own id for this item (Gmail message id, Calendar event id, ...) —
    # lets repeated polls upsert instead of duplicating rows.
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    item_metadata: Mapped[dict] = mapped_column(JSONB, default=dict)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    # Set for items that came from a manual upload; deleting the upload deletes its items.
    upload_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("uploads.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # When the underlying thing happened (email sent, event start), as opposed to created_at
    # (when we indexed it). Falls back to index time when the provider gives no usable date.
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def is_first_chunk() -> ColumnElement[bool]:
    """Filter for one row per memory: a long upload is stored as several chunks, and only its
    first counts when browsing or tallying. Rows with no chunk_index (emails, events,
    bookmarks) are always their own first chunk.
    """
    return func.coalesce(MemoryItem.item_metadata["chunk_index"].as_integer(), 0) == 0
