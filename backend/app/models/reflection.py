import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

CATEGORIES = ("work", "relationships", "schedule", "finance", "learning", "personal", "inbox")
TONES = ("positive", "neutral", "watch")


class Reflection(Base):
    """One pattern the twin noticed in the user's memories (services/reflections.py).

    Generated in batches (`batch_id`) by the LLM from computed facts plus numbered memories,
    and only kept when it cites the memories it's based on — `evidence` snapshots them
    (title/source/date) so the card still makes sense if a memory is later forgotten.
    """

    __tablename__ = "reflections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    tone: Mapped[str] = mapped_column(String(20), nullable=False)
    headline: Mapped[str] = mapped_column(String(160), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    evidence: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Set once the user opens "Talk it through", so a second click reopens the same thread.
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
