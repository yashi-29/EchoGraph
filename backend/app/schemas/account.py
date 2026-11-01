from datetime import datetime

from pydantic import BaseModel


class SourceDataSummary(BaseModel):
    source: str
    memories: int
    connected: bool
    account_email: str | None
    oldest: datetime | None
    newest: datetime | None


class DataSummary(BaseModel):
    """What the twin currently stores for this user — backs Settings → Privacy & data."""

    total_memories: int
    conversations: int
    messages: int
    sources: list[SourceDataSummary]


class DeletedCount(BaseModel):
    deleted: int
