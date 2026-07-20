"""Shared types, constants, and exception hierarchy for core/ and storage/.

This module centralises cross-module dataclasses to avoid circular imports.
Both ``core/`` and ``storage/`` import from here; ``storage/`` must NOT
import from any other ``core/`` module (architecture §1.3 layering rule).

Defined here:
    - GraphRAG delimiter constants (TUPLE_DELIMITER, RECORD_DELIMITER, COMPLETION_DELIMITER)
    - EdgeConfidence re-export (from adapters.protocols)
    - DEFAULT_ENTITY_TYPES for the car-sales domain
    - GraphNode / GraphEdge / GraphSnapshot (graph layer dataclasses)
    - VectorSearchHit (vector store result)
    - AudioGraphyError hierarchy (ParseError / StorageError / PipelineError)
    - Confidence upgrade helper
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from audio_graphy.adapters.protocols import EdgeConfidence

# ============================================================
# GraphRAG delimiter protocol constants
# ============================================================

TUPLE_DELIMITER: str = "<|>"
"""Field separator within a single record (GraphRAG tuple delimiter)."""

RECORD_DELIMITER: str = "##"
"""Separator between records (entities / relations)."""

COMPLETION_DELIMITER: str = "<|COMPLETE|>"
"""End-of-output marker appended by the LLM."""

# ============================================================
# Domain defaults
# ============================================================

DEFAULT_ENTITY_TYPES: tuple[str, ...] = (
    "客户",
    "坐席",
    "车型",
    "价格方案",
    "金融政策",
    "优惠权益",
    "竞品",
    "预约事件",
)
"""Default entity types for the car-sales domain (DESIGN.md §5.1)."""

# ============================================================
# Shared dataclasses (frozen + slots, consistent with adapters.protocols)
# ============================================================


@dataclass(frozen=True, slots=True)
class GraphNode:
    """A merged entity node in the knowledge graph.

    Attributes:
        entity_id: Normalised entity name used as the NetworkX node key.
        name: Display name of the entity.
        type: Domain type chosen by majority vote across chunks.
        description: De-duplicated, concatenated description.
        source_ids: Provenance — list of ``"{recording_id}_{chunk_id}"``.
        recording_ids: Recordings in which this entity appears.
        degree: Number of connected edges (for god-node ranking).
    """

    entity_id: str
    name: str
    type: str
    description: str
    source_ids: list[str]
    recording_ids: list[int]
    degree: int = 0


@dataclass(frozen=True, slots=True)
class GraphEdge:
    """A merged relation edge in the knowledge graph.

    Attributes:
        source: Source entity_id.
        target: Target entity_id.
        relation: Relation description (e.g. "推荐", "询问").
        weight: Accumulated weight (more mentions → stronger).
        confidence: EXTRACTED / INFERRED / AMBIGUOUS.
        confidence_score: 1.0 for EXTRACTED; 0.0–1.0 for INFERRED; None for AMBIGUOUS.
        source_ids: Provenance — list of ``"{recording_id}_{chunk_id}"``.
    """

    source: str
    target: str
    relation: str
    weight: float
    confidence: EdgeConfidence
    confidence_score: float | None
    source_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class GraphSnapshot:
    """Snapshot of the graph after building from extractions.

    Attributes:
        nodes: All merged GraphNode objects.
        edges: All merged GraphEdge objects.
        total_entities: Count of unique entities.
        total_relations: Count of unique relations.
        cross_recording_entities: Number of entities spanning ≥ 2 recordings.
    """

    nodes: list[GraphNode]
    edges: list[GraphEdge]
    total_entities: int
    total_relations: int
    cross_recording_entities: int = 0


@dataclass(frozen=True, slots=True)
class VectorSearchHit:
    """A single brute-force cosine search result.

    Attributes:
        id: entity_id (str) or chunk_id (int) depending on the table searched.
        score: Cosine similarity in [-1, 1].
    """

    id: str | int
    score: float


# ============================================================
# Exception hierarchy
# ============================================================


class AudioGraphyError(Exception):
    """Base exception for all AudioGraphy custom errors."""


class ParseError(AudioGraphyError):
    """Raised when LLM output cannot be parsed (GraphRAG delimiter protocol)."""


class StorageError(AudioGraphyError):
    """Raised by storage layer (file_index / mysql_vector / graph_networkx)."""


class PipelineError(AudioGraphyError):
    """Raised by pipeline stages (chunker / extractor / graph / retrieval / rerank)."""


# ============================================================
# Confidence helpers
# ============================================================

_CONFIDENCE_RANK: dict[EdgeConfidence, int] = {
    "AMBIGUOUS": 0,
    "INFERRED": 1,
    "EXTRACTED": 2,
}


def upgrade_confidence(existing: EdgeConfidence, incoming: EdgeConfidence) -> EdgeConfidence:
    """Return the higher of two confidence levels.

    Upgrade rule (architecture §1.7):
        EXTRACTED > INFERRED > AMBIGUOUS
        An existing EXTRACTED never downgrades.

    Args:
        existing: Current confidence on the edge.
        incoming: New confidence being merged in.

    Returns:
        The higher confidence level.
    """
    if _CONFIDENCE_RANK[incoming] > _CONFIDENCE_RANK[existing]:
        return incoming
    return existing


def normalize_confidence_score(confidence: EdgeConfidence, weight: float) -> float | None:
    """Compute the confidence_score for a given confidence tag.

    - EXTRACTED → 1.0
    - INFERRED → normalised weight (0.0 < score < 1.0)
    - AMBIGUOUS → None

    Args:
        confidence: The edge confidence tag.
        weight: Raw accumulated weight (used for INFERRED normalisation).

    Returns:
        The confidence score, or None for AMBIGUOUS.
    """
    if confidence == "EXTRACTED":
        return 1.0
    if confidence == "INFERRED":
        # Normalise: weight is always ≥ 1.0 after accumulation;
        # map to (0, 1) via weight / (weight + 1).
        return round(weight / (weight + 1.0), 4) if weight > 0 else 0.5
    return None


# ============================================================
# Utility: serialise / deserialise list attributes for GraphML
# ============================================================


def _list_to_str(items: list[Any]) -> str:
    """Serialise a list to a JSON string for GraphML attribute storage."""
    import json

    return json.dumps(items, ensure_ascii=False)


def _str_to_list(s: str) -> list[Any]:
    """Deserialise a JSON string back to a list (GraphML attribute → Python)."""
    import json

    if not s:
        return []
    try:
        return list(json.loads(s))
    except (json.JSONDecodeError, TypeError):
        return []
