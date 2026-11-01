from datetime import datetime
from typing import Literal

from pydantic import BaseModel

NodeType = Literal["you", "person", "organization", "topic"]


class GraphItem(BaseModel):
    title: str
    source: str
    occurred_at: datetime


class GraphNode(BaseModel):
    id: str
    type: NodeType
    label: str
    # person/organization → email address or domain; topic → its top keywords joined.
    subtitle: str | None
    count: int  # memories behind this node
    first_seen: datetime | None
    last_seen: datetime | None
    sources: dict[str, int]
    keywords: list[str]
    recent: list[GraphItem]


class GraphEdge(BaseModel):
    source: str
    target: str
    weight: int  # memories the two ends share


class GraphResponse(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    total_memories: int
    clustered_memories: int  # memories with an embedding, i.e. eligible for topics
    generated_at: datetime
