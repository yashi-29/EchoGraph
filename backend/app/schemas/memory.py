import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


class TimelineItem(BaseModel):
    id: uuid.UUID
    source: str
    title: str
    body: str
    occurred_at: datetime
    all_day: bool
    # Source-specific display details: gmail → from; google_calendar → end, location,
    # attendee_count, html_link; document/voice_note → upload_id, filename, pages,
    # chunk_index, chunk_count; bookmark → url, folder.
    details: dict[str, Any]


class SourceCount(BaseModel):
    source: str
    count: int


class TimelinePage(BaseModel):
    items: list[TimelineItem]
    # Opaque cursor for the next (older) page; None when there's nothing older.
    next_cursor: str | None
    # First page only (empty on later pages): the next few future items, e.g. calendar events.
    upcoming: list[TimelineItem]
    # First page only: per-source totals across all of the user's memories, for filter chips.
    sources: list[SourceCount]


class SearchResult(TimelineItem):
    # `body` holds a snippet centred on the first matching term rather than the text's start.
    match: Literal["keyword", "semantic", "both"]
    # Where to open the original (Gmail message, Calendar event, bookmarked page), if known.
    url: str | None


class SearchResponse(BaseModel):
    query: str
    terms: list[str]  # what the frontend highlights
    results: list[SearchResult]
    total: int
    has_more: bool
    # False when meaning-based search couldn't run (e.g. embedding rate limit) — keyword only.
    semantic: bool


class SearchSuggestions(BaseModel):
    suggestions: list[str]
    sources: list[SourceCount]
    total: int
