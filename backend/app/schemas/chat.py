import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.actions import PendingActionRead


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    # Omit to start a new conversation; history is loaded server-side from this thread.
    conversation_id: uuid.UUID | None = None
    # The browser's IANA zone (e.g. "Asia/Kolkata") so "tomorrow at 5" or "28th Sept" resolve
    # to the user's own calendar day. Missing or unknown → UTC.
    timezone: str | None = Field(default=None, max_length=64)


class Citation(BaseModel):
    source: str
    external_id: str
    title: str
    snippet: str
    occurred_at: datetime


class ChatResponse(BaseModel):
    conversation_id: uuid.UUID
    conversation_title: str
    message_id: uuid.UUID
    reply: str
    citations: list[Citation]
    # Every side-effecting action drafted this turn, each awaiting its own confirm.
    pending_actions: list[PendingActionRead] = []


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: str
    content: str
    citations: list[Citation]
    pending_actions: list[PendingActionRead]
    created_at: datetime


class ConversationSummary(BaseModel):
    id: uuid.UUID
    title: str
    preview: str | None
    updated_at: datetime


class ConversationDetail(BaseModel):
    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime
    messages: list[MessageRead]


class ConversationUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
