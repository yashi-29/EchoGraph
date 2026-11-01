import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, LargeBinary, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, deferred, mapped_column

from app.db.base import Base

# Every upload kind doubles as the `source` of the memory items it produces.
UPLOAD_KINDS = ("document", "image", "voice_note", "bookmark")


class Upload(Base):
    """A file the user added by hand (PDF, text, image, voice note, bookmark export).

    The original bytes are kept so a later action (e.g. "email my NOC to Aditya") can attach
    the real file; its indexed chunks live in memory_items, linked back via upload_id and
    deleted with it.
    """

    __tablename__ = "uploads"
    # Re-uploading the exact same file is rejected instead of indexing it twice.
    __table_args__ = (UniqueConstraint("user_id", "sha256", name="uq_upload_user_sha256"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # Deferred: listing uploads must not pull every file's bytes out of the database.
    data: Mapped[bytes] = deferred(mapped_column(LargeBinary, nullable=False))
    # Kind-specific facts about the file: pages, truncated, bookmark count, ...
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
