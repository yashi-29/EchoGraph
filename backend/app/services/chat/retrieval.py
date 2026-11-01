import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.memory_item import MemoryItem
from app.services.embedding.voyage import embed_query

_DEFAULT_LIMIT = 10
_MAX_LIMIT = 50


async def search_memories(
    user_id: uuid.UUID,
    db: AsyncSession,
    *,
    query: str | None = None,
    source: str | None = None,
    limit: int = _DEFAULT_LIMIT,
    order: str = "relevance",
    since: datetime | None = None,
) -> list[MemoryItem]:
    """Backs the search_memory tool. `order`:
    - "recent": most recently indexed first — for "my last N emails"-style requests.
    - "relevance": semantic similarity to `query` — for "what did X say about Y" questions.
    `since` enforces the user's memory window: older items are never returned.
    """
    bounded_limit = max(1, min(limit, _MAX_LIMIT))
    stmt = select(MemoryItem).where(MemoryItem.user_id == user_id)
    if source:
        stmt = stmt.where(MemoryItem.source == source)
    if since is not None:
        stmt = stmt.where(MemoryItem.occurred_at >= since)

    if order == "recent":
        # occurred_at = when the thing actually happened (email sent, event start), not
        # created_at (when we indexed it) — ingestion runs in batches, so indexing order
        # does not track real recency at all.
        stmt = stmt.order_by(MemoryItem.occurred_at.desc())
    else:
        stmt = stmt.where(MemoryItem.embedding.is_not(None))
        query_vector = await embed_query(query or "")
        stmt = stmt.order_by(MemoryItem.embedding.cosine_distance(query_vector))

    stmt = stmt.limit(bounded_limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())
