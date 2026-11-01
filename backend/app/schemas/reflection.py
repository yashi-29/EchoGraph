import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class EvidenceItem(BaseModel):
    memory_id: uuid.UUID
    external_id: str
    source: str
    title: str
    snippet: str = ""
    occurred_at: datetime


class ReflectionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    category: str
    tone: str  # positive | neutral | watch
    headline: str
    body: str
    tags: list[str]
    evidence: list[EvidenceItem]
    period_start: datetime
    period_end: datetime
    conversation_id: uuid.UUID | None
    dismissed_at: datetime | None
    created_at: datetime


class ReflectionList(BaseModel):
    reflections: list[ReflectionRead]
    memory_count: int
    min_memories: int
    last_generated_at: datetime | None
    # When the weekly job will next produce a batch (None until the first one exists).
    next_scheduled_at: datetime | None
    # "Refresh now" is rate-limited; None means it's available immediately.
    can_generate_at: datetime | None


class GenerateRequest(BaseModel):
    # The browser's IANA zone, so weekday/hour patterns use the user's local time.
    timezone: str | None = Field(default=None, max_length=64)


class DiscussResponse(BaseModel):
    conversation_id: uuid.UUID
