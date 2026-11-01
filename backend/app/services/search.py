"""Hybrid memory search for the Search page (GET /memory/search).

Two retrievers run side by side and are merged with Reciprocal Rank Fusion:
- keyword: every query term must appear in the memory text (ILIKE), newest first;
- semantic: cosine similarity between the query embedding and each memory's embedding,
  keeping only results close to the best match (a nearest neighbour always exists, even for
  gibberish, so an absolute floor plus a band below the top score filters the noise).
If embedding the query fails (no key, Voyage rate limit, network) the search degrades to
keyword-only and says so, rather than erroring.
"""

import html
import re
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field

from sqlalchemy import ColumnElement, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.memory_item import MemoryItem
from app.services.embedding.voyage import embed_query

CANDIDATES = 100  # per retriever, before fusion
MAX_TERMS = 8
# Similarity cutoffs, calibrated on voyage-4 vectors of real mail/calendar data: a genuine
# query's best match scores ~0.52–0.61 with related items trailing to ~0.39, while gibberish
# or off-topic queries ("zzqqxx", "weather in paris") top out at ~0.26–0.32.
SEMANTIC_FLOOR = 0.36
SEMANTIC_BAND = 0.18
_RRF_K = 60
_SNIPPET_CHARS = 240

_TERM = re.compile(r'"([^"]+)"|(\S+)')
_embedding_cache: OrderedDict[str, list[float]] = OrderedDict()
_EMBEDDING_CACHE_SIZE = 256


@dataclass
class Hit:
    item: MemoryItem
    score: float = 0.0
    matched_by: set[str] = field(default_factory=set)  # {"keyword", "semantic"}

    @property
    def match(self) -> str:
        return "both" if len(self.matched_by) == 2 else next(iter(self.matched_by))


def parse_terms(query: str) -> list[str]:
    """Words and "quoted phrases", lowercased, deduped. Single characters are dropped unless
    they're all there is (searching for "a" in every memory helps nobody)."""
    raw = [(phrase or word).strip().lower() for phrase, word in _TERM.findall(query)]
    terms = list(dict.fromkeys(t for t in raw if t))
    longer = [t for t in terms if len(t) > 1]
    return (longer or terms)[:MAX_TERMS]


async def query_embedding(query: str) -> list[float] | None:
    """Embeds the query, memoised per process — the free Voyage tier allows only a few
    requests a minute, and people re-run the same searches. None on any failure."""
    key = " ".join(query.lower().split())
    if key in _embedding_cache:
        _embedding_cache.move_to_end(key)
        return _embedding_cache[key]
    try:
        vector = await embed_query(key)
    except Exception:
        return None
    _embedding_cache[key] = vector
    if len(_embedding_cache) > _EMBEDDING_CACHE_SIZE:
        _embedding_cache.popitem(last=False)
    return vector


def _searchable_text() -> ColumnElement[str]:
    """The memory text plus the metadata people search by but that isn't in the text itself:
    who sent an email, who was at an event and where, a document's filename."""
    meta = MemoryItem.item_metadata
    return func.concat_ws(
        " ",
        MemoryItem.content,
        meta["from"].astext,
        meta["attendees"].astext,  # JSON array text — addresses still match as substrings
        meta["location"].astext,
        meta["filename"].astext,
    )


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def hybrid_search(
    user_id: uuid.UUID, db: AsyncSession, query: str, source: str | None = None
) -> tuple[list[Hit], bool]:
    """Ranked hits for `query`, plus whether the semantic side ran."""
    terms = parse_terms(query)
    base = [MemoryItem.user_id == user_id]
    if source:
        base.append(MemoryItem.source == source)

    hits: dict[uuid.UUID, Hit] = {}

    def add(item: MemoryItem, rank: int, kind: str) -> None:
        hit = hits.setdefault(item.id, Hit(item))
        hit.score += 1 / (_RRF_K + rank)
        hit.matched_by.add(kind)

    if terms:
        searchable = _searchable_text()
        keyword_rows = await db.execute(
            select(MemoryItem)
            .where(and_(*base, *(searchable.ilike(_like(t), escape="\\") for t in terms)))
            .order_by(MemoryItem.occurred_at.desc())
            .limit(CANDIDATES)
        )
        for rank, item in enumerate(keyword_rows.scalars()):
            add(item, rank, "keyword")

    vector = await query_embedding(query)
    if vector is not None:
        distance = MemoryItem.embedding.cosine_distance(vector)
        semantic_rows = (
            await db.execute(
                select(MemoryItem, distance)
                .where(*base, MemoryItem.embedding.is_not(None))
                .order_by(distance)
                .limit(CANDIDATES)
            )
        ).all()
        if semantic_rows:
            best = 1 - semantic_rows[0][1]
            cutoff = max(SEMANTIC_FLOOR, best - SEMANTIC_BAND)
            rank = 0
            for item, dist in semantic_rows:
                if 1 - dist < cutoff:
                    break
                add(item, rank, "semantic")
                rank += 1

    ranked = sorted(hits.values(), key=lambda h: h.score, reverse=True)
    # A long upload is many chunks; show only its best-matching one.
    seen_uploads: set[uuid.UUID] = set()
    collapsed = []
    for hit in ranked:
        upload_id = hit.item.upload_id
        if upload_id is not None:
            if upload_id in seen_uploads:
                continue
            seen_uploads.add(upload_id)
        collapsed.append(hit)
    return collapsed, vector is not None


def snippet(text: str, terms: list[str]) -> str:
    """~240 chars of `text` around the first query term (or its start if none appear)."""
    text = " ".join(html.unescape(text).split())
    if len(text) <= _SNIPPET_CHARS:
        return text
    lower = text.lower()
    positions = [p for p in (lower.find(t) for t in terms) if p >= 0]
    start = max(0, min(positions) - 80) if positions else 0
    if start:
        space = text.find(" ", start)
        start = space + 1 if 0 <= space < start + 20 else start
    end = min(len(text), start + _SNIPPET_CHARS)
    if end < len(text):
        space = text.rfind(" ", start, end)
        end = space if space > start + _SNIPPET_CHARS // 2 else end
    return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")
