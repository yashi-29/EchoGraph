import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class UploadRead(BaseModel):
    id: uuid.UUID
    kind: str  # "document" | "image" | "voice_note" | "bookmark" — also the memories' source
    filename: str
    content_type: str
    size_bytes: int
    # Memory items currently linked to this file (chunks, or bookmarks it still owns).
    items: int
    # Kind-specific: pages / truncated (documents), transcript_chars (voice notes),
    # bookmarks (bookmark exports).
    details: dict[str, Any]
    created_at: datetime
