"""Assembles the knowledge graph for one user from their stored memories.

Nodes: the user ("you"), the people and organizations their memories involve (entities.py),
and topics (topics.py). Edges carry how many memories the two ends share. The result is
cached in `graph_snapshots` and rebuilt only when the memories change.
"""

import hashlib
import html
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import combinations

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.connector import Connector
from app.models.graph_snapshot import GraphSnapshot
from app.models.memory_item import MemoryItem
from app.models.user import User
from app.schemas.graph import GraphEdge, GraphItem, GraphNode, GraphResponse
from app.services.graph.entities import Entity, classify_address
from app.services.graph.topics import cluster_count, cluster_keywords, kmeans, llm_labels

# Bump when the build logic changes so every cached snapshot is rebuilt.
GRAPH_VERSION = "2"
MAX_ITEMS = 2000  # most recent memories considered
MAX_PEOPLE = 8
MAX_ENTITIES = 12  # people first, organizations fill the rest
RECENT_PER_NODE = 5
_TITLE_CHARS = 120


@dataclass
class _Item:
    source: str
    title: str
    text: str
    occurred_at: datetime
    embedding: np.ndarray | None
    entities: list[Entity] = field(default_factory=list)


@dataclass
class _Group:
    indices: list[int] = field(default_factory=list)
    names: Counter = field(default_factory=Counter)


def _title(content: str) -> str:
    return html.unescape(content.partition("\n\n")[0].strip())[:_TITLE_CHARS] or "(untitled)"


def _node(
    node_id: str,
    node_type: str,
    label: str,
    subtitle: str | None,
    indices: list[int],
    items: list[_Item],
    keywords: list[str] | None = None,
) -> GraphNode:
    members = [items[i] for i in indices]  # items are newest-first, so members are too
    return GraphNode(
        id=node_id,
        type=node_type,  # type: ignore[arg-type]
        label=label,
        subtitle=subtitle,
        count=len(members),
        first_seen=members[-1].occurred_at if members else None,
        last_seen=members[0].occurred_at if members else None,
        sources=dict(Counter(m.source for m in members)),
        keywords=keywords or [],
        recent=[
            GraphItem(title=m.title, source=m.source, occurred_at=m.occurred_at)
            for m in members[:RECENT_PER_NODE]
        ],
    )


async def user_addresses(user: User, db: AsyncSession) -> set[str]:
    """The user's own email addresses (login + every connected account) — never graph nodes."""
    emails = (
        await db.execute(
            select(Connector.external_account_email).where(Connector.user_id == user.id)
        )
    ).scalars()
    return {user.email.lower(), *(e.lower() for e in emails if e)}


async def _fingerprint(user_id: uuid.UUID, self_addresses: set[str], db: AsyncSession) -> str:
    stats = (
        await db.execute(
            select(
                func.count(),
                func.count(MemoryItem.embedding),
                func.max(MemoryItem.created_at),
                func.min(MemoryItem.created_at),
                func.max(MemoryItem.occurred_at),
                func.coalesce(func.sum(func.length(MemoryItem.content)), 0),
            ).where(MemoryItem.user_id == user_id)
        )
    ).one()
    raw = f"{GRAPH_VERSION}|{'|'.join(map(str, stats))}|{','.join(sorted(self_addresses))}"
    return hashlib.sha256(raw.encode()).hexdigest()


async def build_graph(
    user_id: uuid.UUID, self_addresses: set[str], db: AsyncSession
) -> GraphResponse:
    total = await db.scalar(
        select(func.count()).select_from(MemoryItem).where(MemoryItem.user_id == user_id)
    )
    rows = await db.execute(
        select(
            MemoryItem.source,
            MemoryItem.content,
            MemoryItem.item_metadata,
            MemoryItem.occurred_at,
            MemoryItem.embedding,
        )
        .where(MemoryItem.user_id == user_id)
        .order_by(MemoryItem.occurred_at.desc(), MemoryItem.id.desc())
        .limit(MAX_ITEMS)
    )
    items: list[_Item] = []
    for source, content, metadata, occurred_at, embedding in rows:
        meta = metadata or {}
        item = _Item(
            source=source,
            title=_title(content),
            text=content[:400],
            occurred_at=occurred_at,
            embedding=np.asarray(embedding, dtype=np.float32) if embedding is not None else None,
        )
        raw_addresses = [meta.get("from")] if source == "gmail" else meta.get("attendees") or []
        seen: set[str] = set()
        for raw in raw_addresses:
            entity = classify_address(str(raw or ""), self_addresses)
            if entity and entity.key not in seen:
                seen.add(entity.key)
                item.entities.append(entity)
        items.append(item)

    # --- people & organizations -----------------------------------------------------------
    groups: dict[str, _Group] = defaultdict(_Group)
    kinds: dict[str, Entity] = {}
    for i, item in enumerate(items):
        for entity in item.entities:
            groups[entity.key].indices.append(i)
            groups[entity.key].names[entity.name] += 1
            kinds[entity.key] = entity
    people = sorted(
        (k for k in groups if kinds[k].kind == "person"), key=lambda k: -len(groups[k].indices)
    )[:MAX_PEOPLE]
    orgs = sorted(
        (k for k in groups if kinds[k].kind == "organization"),
        key=lambda k: -len(groups[k].indices),
    )[: MAX_ENTITIES - len(people)]
    chosen = people + orgs

    # --- topics -----------------------------------------------------------------------------
    embedded = [i for i, item in enumerate(items) if item.embedding is not None]
    topic_of: dict[int, str] = {}
    topic_nodes: list[GraphNode] = []
    k = cluster_count(len(embedded))
    if k:
        assignment = kmeans(np.stack([items[i].embedding for i in embedded]), k)  # type: ignore[misc]
        clusters = [[embedded[pos] for pos in np.flatnonzero(assignment == c)] for c in range(k)]
        clusters = sorted((c for c in clusters if c), key=len, reverse=True)
        own_words = {part for a in self_addresses for part in a.split("@")[0].split(".")}
        keywords = cluster_keywords(
            [[f"{items[i].title} {items[i].text}" for i in c] for c in clusters], ignore=own_words
        )
        labels = await llm_labels([[items[i].title for i in c] for c in clusters], keywords)
        used: Counter = Counter()
        for n, (members, words, label) in enumerate(zip(clusters, keywords, labels, strict=True)):
            used[label] += 1
            unique_label = label if used[label] == 1 else f"{label} {used[label]}"
            topic_id = f"topic:{n}"
            for i in members:
                topic_of[i] = topic_id
            topic_nodes.append(
                _node(
                    topic_id, "topic", unique_label, ", ".join(words) or None, members, items, words
                )
            )

    # --- nodes --------------------------------------------------------------------------------
    source_count = len({item.source for item in items})
    nodes = [
        _node(
            "you",
            "you",
            "You",
            f"{source_count} {'source' if source_count == 1 else 'sources'}" if items else None,
            list(range(len(items))),
            items,
        )
    ]
    for key in chosen:
        entity = kinds[key]
        nodes.append(
            _node(
                key,
                entity.kind,
                groups[key].names.most_common(1)[0][0],
                entity.address,
                groups[key].indices,
                items,
            )
        )
    nodes += topic_nodes
    nodes[0].count = total or 0

    # --- edges --------------------------------------------------------------------------------
    edges = [GraphEdge(source="you", target=key, weight=len(groups[key].indices)) for key in chosen]
    edges += [GraphEdge(source="you", target=t.id, weight=t.count) for t in topic_nodes]
    for key in chosen:
        per_topic = Counter(topic_of[i] for i in groups[key].indices if i in topic_of)
        if not per_topic:
            continue
        strongest, _ = per_topic.most_common(1)[0]
        for topic_id, shared in per_topic.items():
            if topic_id == strongest or shared >= 2:
                edges.append(GraphEdge(source=key, target=topic_id, weight=shared))
    chosen_set = set(chosen)
    together: Counter = Counter()
    for item in items:
        keys = sorted(e.key for e in item.entities if e.key in chosen_set)
        together.update(combinations(keys, 2))
    edges += [GraphEdge(source=a, target=b, weight=n) for (a, b), n in together.items()]

    return GraphResponse(
        nodes=nodes,
        edges=edges,
        total_memories=total or 0,
        clustered_memories=len(embedded),
        generated_at=datetime.now(UTC),
    )


async def get_graph(user: User, db: AsyncSession) -> dict:
    """The user's graph as JSON-ready data, from cache when their memories haven't changed."""
    self_addresses = await user_addresses(user, db)
    fingerprint = await _fingerprint(user.id, self_addresses, db)
    snapshot = await db.get(GraphSnapshot, user.id)
    if snapshot is not None and snapshot.fingerprint == fingerprint:
        return snapshot.data

    data = (await build_graph(user.id, self_addresses, db)).model_dump(mode="json")
    stmt = insert(GraphSnapshot).values(user_id=user.id, fingerprint=fingerprint, data=data)
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[GraphSnapshot.user_id],
            set_={"fingerprint": fingerprint, "data": data, "created_at": func.now()},
        )
    )
    await db.commit()
    return data
