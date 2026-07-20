"""Graph schemas: nodes, edges, explore/entity/subgraph/path responses.

See: docs/m3-prd.md §4.5.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class GraphNodeResponse(BaseModel):
    """A graph node."""

    id: str = Field(description="Entity ID (normalised name)")
    label: str = Field(description="Display name")
    type: str
    description: str | None = None
    degree: int = 0
    source_ids: list[str] = Field(default_factory=list)
    recording_ids: list[int] = Field(default_factory=list)
    recorded_at_range: list[datetime] | None = None


class GraphEdgeResponse(BaseModel):
    """A graph edge."""

    source: str
    target: str
    relation: str
    weight: float = 1.0
    confidence: str = Field(description="EXTRACTED / INFERRED / AMBIGUOUS")
    confidence_score: float | None = None
    source_ids: list[str] = Field(default_factory=list)


class ExploreResponse(BaseModel):
    """GET /graph/explore and /graph/subgraph response."""

    nodes: list[GraphNodeResponse]
    edges: list[GraphEdgeResponse]
    total_nodes: int
    total_edges: int


class NeighborResponse(BaseModel):
    """A neighbor node in entity detail."""

    id: str
    label: str
    type: str
    relation: str
    weight: float = 1.0
    confidence: str = "EXTRACTED"


class EntityDetailResponse(BaseModel):
    """GET /graph/entity/{name} response."""

    node: GraphNodeResponse
    neighbors: list[NeighborResponse]
    relation_counts: dict[str, int]


class PathResponse(BaseModel):
    """GET /graph/path response."""

    path: list[str]
    length: int
    edges: list[GraphEdgeResponse]
