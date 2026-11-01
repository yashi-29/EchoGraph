import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Accepted values for the twin-behavior preferences (validated in schemas/user.py too).
RESPONSE_TONES = ("balanced", "concise", "warm")
MEMORY_WINDOWS_DAYS = (90, 365)  # None = all time


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Last IANA zone the browser reported (e.g. "Asia/Kolkata"); lets background jobs like
    # weekly reflections reason about the user's own weekdays and hours. None → UTC.
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # --- preferences read by the chat pipeline (services/chat/groq_chat.py) ---
    # Off → search results reach the model as title/source/date only, never the body text.
    allow_content_in_chat: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    response_tone: Mapped[str] = mapped_column(
        String(20), nullable=False, default="balanced", server_default="balanced"
    )
    # Only memories that happened within this many days are searchable; None = all time.
    memory_window_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
