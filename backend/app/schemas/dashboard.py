from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class MemoryStats(BaseModel):
    total: int
    today: int
    this_week: int
    last_week: int
    # Items with an embedding — i.e. reachable by chat/search retrieval.
    searchable: int


class SourceStats(BaseModel):
    connected: int
    available: int


class ActivityItem(BaseModel):
    kind: Literal["sync", "connected"]
    source: str
    count: int  # new items indexed for "sync"; always 1 for "connected"
    at: datetime


class DashboardSummary(BaseModel):
    email: str
    full_name: str | None
    memories: MemoryStats
    sources: SourceStats
    last_indexed_at: datetime | None
    activity: list[ActivityItem]
