import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PendingAction(Base):
    """A tool call the chat model wants to make that has a real external side effect.

    Created in "pending" status with nothing executed yet. Only a separate,
    explicit user confirmation (POST .../confirm) moves it to "executed" —
    see the safety-critical rule in CONTRIBUTING.md.
    """

    __tablename__ = "pending_actions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    # The assistant reply that proposed this action; one reply can propose several.
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversation_messages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    tool_name: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # pending -> executed | failed, or pending -> cancelled
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
