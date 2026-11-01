import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.connector import Connector
from app.models.memory_item import MemoryItem
from app.services.embedding.voyage import embed_texts
from app.services.ingestion import gmail as gmail_ingestion
from app.services.ingestion import google_calendar as google_calendar_ingestion
from app.services.ingestion.base import NormalizedItem, SyncBatch
from app.services.oauth.tokens import get_valid_access_token


async def _fetch_calendar(access_token: str, sync_state: dict[str, Any]) -> SyncBatch:
    # Calendar re-reads a fixed window every poll, so it has no cursor to carry forward.
    return SyncBatch(await google_calendar_ingestion.fetch_and_normalize(access_token), {})


# Each fetcher gets a valid access token and the connector's last sync_state, and returns
# the items to upsert plus the sync_state to store for the next poll.
_PROVIDER_FETCHERS: dict[str, Callable[[str, dict[str, Any]], Awaitable[SyncBatch]]] = {
    "gmail": gmail_ingestion.fetch_batch,
    "google_calendar": _fetch_calendar,
}


async def _embeddings_for(
    user_id: uuid.UUID, source: str, items: list[NormalizedItem], db: AsyncSession
) -> dict[str, list[float] | None]:
    """Only (re-)embed items that are new or whose content changed since last sync —
    re-embedding unchanged items on every poll would burn through quota for nothing.
    """
    existing = await db.execute(
        select(MemoryItem.external_id, MemoryItem.content, MemoryItem.embedding).where(
            MemoryItem.user_id == user_id,
            MemoryItem.source == source,
            MemoryItem.external_id.in_([item.external_id for item in items]),
        )
    )
    existing_by_id = {row.external_id: row for row in existing}

    needs_embedding = [
        item
        for item in items
        if (existing_row := existing_by_id.get(item.external_id)) is None
        or existing_row.content != item.content
        or existing_row.embedding is None
    ]
    fresh_vectors = (
        await embed_texts([item.content for item in needs_embedding]) if needs_embedding else []
    )

    embeddings: dict[str, list[float] | None] = {
        item.external_id: existing_by_id[item.external_id].embedding
        for item in items
        if item.external_id in existing_by_id
    }
    for item, vector in zip(needs_embedding, fresh_vectors, strict=True):
        embeddings[item.external_id] = vector
    return embeddings


async def store_items(
    user_id: uuid.UUID,
    source: str,
    items: list[NormalizedItem],
    db: AsyncSession,
    *,
    upload_id: uuid.UUID | None = None,
) -> int:
    """Embeds and upserts `items`, then commits. Shared by connector syncs and manual uploads
    (`upload_id` links each item to the file it came from).
    """
    if not items:
        return 0

    embeddings = await _embeddings_for(user_id, source, items, db)

    for item in items:
        values = {
            "user_id": user_id,
            "source": source,
            "external_id": item.external_id,
            "content": item.content,
            "item_metadata": item.item_metadata,
            "embedding": embeddings.get(item.external_id),
            "upload_id": upload_id,
        }
        # Unknown dates fall back to the column's server default (index time) on insert, and
        # never overwrite a date an earlier sync already recorded.
        if item.occurred_at is not None:
            values["occurred_at"] = item.occurred_at
        stmt = insert(MemoryItem).values(**values)
        update_set = {
            "content": stmt.excluded.content,
            "item_metadata": stmt.excluded.item_metadata,
            "embedding": stmt.excluded.embedding,
            # A re-imported bookmark belongs to the newest export that contains it.
            "upload_id": stmt.excluded.upload_id,
        }
        if item.occurred_at is not None:
            update_set["occurred_at"] = stmt.excluded.occurred_at
        stmt = stmt.on_conflict_do_update(
            constraint="uq_memory_item_source_external_id", set_=update_set
        )
        await db.execute(stmt)
    await db.commit()
    return len(items)


async def sync_connector(connector: Connector, db: AsyncSession) -> int:
    fetcher = _PROVIDER_FETCHERS.get(connector.provider)
    if fetcher is None:
        return 0
    access_token = await get_valid_access_token(connector, db)
    batch = await fetcher(access_token, dict(connector.sync_state or {}))
    # The new cursor commits together with the items it covers: if storing fails, the next
    # poll re-fetches the same batch instead of skipping past it.
    connector.sync_state = batch.sync_state
    count = await store_items(connector.user_id, connector.provider, batch.items, db)
    await db.commit()
    return count
