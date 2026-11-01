import base64
import html
import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import ColumnElement, Select, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.graph_snapshot import GraphSnapshot
from app.models.memory_item import MemoryItem, is_first_chunk
from app.models.user import User
from app.schemas.memory import (
    SearchResponse,
    SearchResult,
    SearchSuggestions,
    SourceCount,
    TimelineItem,
    TimelinePage,
)
from app.services.graph.topics import cluster_keywords
from app.services.search import hybrid_search, parse_terms, snippet

router = APIRouter()

_BODY_CHARS = 280
_UPCOMING_LIMIT = 5

_SUGGESTIONS = 6


def _original_url(item: MemoryItem) -> str | None:
    meta = item.item_metadata or {}
    if item.source == "gmail":
        # An attachment's chunks open the email that carried it.
        message_id = meta.get("message_id") or item.external_id
        return f"https://mail.google.com/mail/u/0/#all/{message_id}"
    link = meta.get("html_link") if item.source == "google_calendar" else meta.get("url")
    return link if isinstance(link, str) and link.startswith("https://") else None


@router.get("/search", response_model=SearchResponse)
async def search(
    q: str = Query(..., min_length=1, max_length=200),
    source: str | None = None,
    sort: Literal["relevance", "newest"] = "relevance",
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SearchResponse:
    """Hybrid keyword + meaning search over the user's memories (services/search.py)."""
    query = q.strip()
    if not query:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Empty query")
    hits, semantic = await hybrid_search(current_user.id, db, query, source)
    if sort == "newest":
        hits.sort(key=lambda h: h.item.occurred_at, reverse=True)

    terms = parse_terms(query)
    page = hits[offset : offset + limit]
    results = []
    for hit in page:
        base = _to_timeline_item(hit.item)
        _, _, body = hit.item.content.partition("\n\n")
        results.append(
            SearchResult(
                **base.model_dump(exclude={"body"}),
                body=snippet(body or hit.item.content, terms),
                match=hit.match,  # type: ignore[arg-type]
                url=_original_url(hit.item),
            )
        )
    return SearchResponse(
        query=query,
        terms=terms,
        results=results,
        total=len(hits),
        has_more=offset + limit < len(hits),
        semantic=semantic,
    )


@router.get("/search/suggestions", response_model=SearchSuggestions)
async def search_suggestions(
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> SearchSuggestions:
    """Starting points drawn from the user's own data: topic and people names from their
    cached knowledge graph when it exists, otherwise the commonest words in recent titles.
    Never builds the graph here (that can call the LLM)."""
    source_rows = await db.execute(
        select(MemoryItem.source, func.count())
        .where(MemoryItem.user_id == current_user.id)
        .group_by(MemoryItem.source)
        .order_by(func.count().desc())
    )
    sources = [SourceCount(source=s, count=c) for s, c in source_rows]
    total = sum(s.count for s in sources)

    suggestions: list[str] = []
    snapshot = await db.get(GraphSnapshot, current_user.id)
    if snapshot is not None:
        nodes = [n for n in snapshot.data.get("nodes", []) if n.get("type") != "you"]
        topics = [n["label"] for n in nodes if n.get("type") == "topic"]
        entities = [n["label"] for n in nodes if n.get("type") in ("person", "organization")]
        suggestions = list(dict.fromkeys(topics[:4] + entities))[:_SUGGESTIONS]
    if not suggestions and total:
        contents = (
            await db.execute(
                select(MemoryItem.content)
                .where(MemoryItem.user_id == current_user.id, is_first_chunk())
                .order_by(MemoryItem.occurred_at.desc())
                .limit(300)
            )
        ).scalars()
        titles = [c.partition("\n\n")[0] for c in contents]
        own = {part for part in current_user.email.split("@")[0].split(".")}
        suggestions = cluster_keywords([titles], top=_SUGGESTIONS, ignore=own)[0]
    return SearchSuggestions(suggestions=suggestions, sources=sources, total=total)


def _encode_cursor(item: MemoryItem) -> str:
    raw = f"{item.occurred_at.isoformat()}|{item.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        occurred_at, item_id = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        return datetime.fromisoformat(occurred_at), uuid.UUID(item_id)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid cursor") from None


def _to_timeline_item(item: MemoryItem) -> TimelineItem:
    # Ingestion stores content as "<title>\n\n<body>" for every source so far.
    title, _, body = item.content.partition("\n\n")
    # Gmail snippets arrive HTML-escaped (&#39; etc).
    body = html.unescape(body.strip())
    if len(body) > _BODY_CHARS:
        body = body[:_BODY_CHARS].rstrip() + "…"
    meta = item.item_metadata or {}

    details: dict[str, object] = {}
    if item.source == "gmail":
        details["from"] = meta.get("from")
        if meta.get("kind") == "attachment":
            details["filename"] = meta.get("filename")
            details["email_subject"] = meta.get("email_subject")
            details["chunk_index"] = meta.get("chunk_index")
            details["chunk_count"] = meta.get("chunk_count")
        elif meta.get("attachments"):
            details["attachments"] = [a.get("filename") for a in meta["attachments"]]
    elif item.source == "google_calendar":
        details["end"] = meta.get("end")
        details["location"] = meta.get("location")
        details["attendee_count"] = len(meta.get("attendees") or [])
        details["html_link"] = meta.get("html_link")
    elif item.source in ("document", "image", "voice_note"):
        details["upload_id"] = str(item.upload_id) if item.upload_id else None
        details["filename"] = meta.get("filename")
        details["pages"] = meta.get("pages")
        if item.source == "image":
            details["width"] = meta.get("width")
            details["height"] = meta.get("height")
            details["camera"] = meta.get("camera")
        details["chunk_index"] = meta.get("chunk_index")
        details["chunk_count"] = meta.get("chunk_count")
    elif item.source == "bookmark":
        details["url"] = meta.get("url")
        details["folder"] = meta.get("folder")

    return TimelineItem(
        id=item.id,
        source=item.source,
        title=html.unescape(title.strip()) or "(untitled)",
        body=body,
        occurred_at=item.occurred_at,
        all_day=bool(meta.get("all_day")),
        details=details,
    )


def _filtered(
    stmt: Select[tuple[MemoryItem]], user_id: uuid.UUID, source: str | None, q: str | None
) -> Select[tuple[MemoryItem]]:
    conditions: list[ColumnElement[bool]] = [MemoryItem.user_id == user_id]
    if source:
        conditions.append(MemoryItem.source == source)
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append(MemoryItem.content.ilike(f"%{escaped}%", escape="\\"))
    else:
        # A long upload is stored as many chunks; browsing shows it once (its first chunk),
        # while a search shows whichever chunks actually match.
        conditions.append(is_first_chunk())
    return stmt.where(*conditions)


@router.get("/timeline", response_model=TimelinePage)
async def timeline(
    source: str | None = None,
    q: str | None = Query(None, max_length=200),
    cursor: str | None = None,
    limit: int = Query(30, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TimelinePage:
    """Newest-first feed of things that have already happened, keyset-paginated on
    (occurred_at, id). Future items (upcoming calendar events) come back separately.
    """
    now = datetime.now(UTC)
    q = q.strip() if q else None

    past = _filtered(select(MemoryItem), current_user.id, source, q).where(
        MemoryItem.occurred_at <= now
    )
    if cursor:
        before_at, before_id = _decode_cursor(cursor)
        past = past.where(tuple_(MemoryItem.occurred_at, MemoryItem.id) < (before_at, before_id))
    rows = (
        (
            await db.execute(
                past.order_by(MemoryItem.occurred_at.desc(), MemoryItem.id.desc()).limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    page, has_more = rows[:limit], len(rows) > limit

    upcoming: list[MemoryItem] = []
    sources: list[SourceCount] = []
    if not cursor:
        upcoming = list(
            (
                await db.execute(
                    _filtered(select(MemoryItem), current_user.id, source, q)
                    .where(MemoryItem.occurred_at > now)
                    .order_by(MemoryItem.occurred_at, MemoryItem.id)
                    .limit(_UPCOMING_LIMIT)
                )
            )
            .scalars()
            .all()
        )
        source_rows = await db.execute(
            select(MemoryItem.source, func.count())
            .where(MemoryItem.user_id == current_user.id, is_first_chunk())
            .group_by(MemoryItem.source)
            .order_by(func.count().desc())
        )
        sources = [SourceCount(source=s, count=c) for s, c in source_rows]

    return TimelinePage(
        items=[_to_timeline_item(item) for item in page],
        next_cursor=_encode_cursor(page[-1]) if has_more else None,
        upcoming=[_to_timeline_item(item) for item in upcoming],
        sources=sources,
    )
