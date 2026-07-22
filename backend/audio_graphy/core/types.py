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
    - M9 exception subtree (BiTemporal / Leiden / Compression / SpeakerLinkerFuzzy)
    - Confidence upgrade helper
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
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
    # M7 — speaker entity (LLM-extracted when Chunker populates speaker labels).
    # Treated identically to other entities for graph merging; SpeakerLinker
    # (§8) handles the cross-recording voiceprint linkage separately.
    "说话人",
)
"""Default entity types for the car-sales domain (DESIGN.md §5.1)."""

# M7 — speaker-specific edge relations (architecture §7.2).
SPEAKER_EDGE_SPEAKS_IN = "speaks_in"
SPEAKER_EDGE_MENTIONS = "mentions"
SPEAKER_EDGE_RECOMMENDS = "recommends"
SPEAKER_EDGE_ASKS = "asks"

# M7 — speaker ambiguity tag values (architecture §7.4).
AMBIGUITY_TAG_NONE: str | None = None
AMBIGUITY_TAG_AMBIGUOUS = "AMBIGUOUS"
AMBIGUITY_TAG_PENDING = "PENDING_REVIEW"

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
        expired_at: M9 bi-temporal — when this node was logically deleted
            (Q3 soft-delete during compression). NULL = live.
    """

    entity_id: str
    name: str
    type: str
    description: str
    source_ids: list[str]
    recording_ids: list[int]
    degree: int = 0
    # M9 Q3 Compression soft-delete timestamp (NULL = live node).
    expired_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class GraphEdge:
    """A merged relation edge in the knowledge graph.

    M9 extends this dataclass with bi-temporal timestamps following the
    Graphiti paradigm (architecture §6). All four M9 fields default to
    ``None`` / sensible values so M1-M8 callers remain source-compatible.

    Attributes:
        source: Source entity_id.
        target: Target entity_id.
        relation: Relation description (e.g. "推荐", "询问").
        weight: Accumulated weight (more mentions → stronger).
        confidence: EXTRACTED / INFERRED / AMBIGUOUS.
        confidence_score: 1.0 for EXTRACTED; 0.0–1.0 for INFERRED; None for AMBIGUOUS.
        source_ids: Provenance — list of ``"{recording_id}_{chunk_id}"``.

    M9 bi-temporal fields (architecture §6, Q1 dual-track):
        valid_at: When the relation became true in the real world
            (defaults to ``created_at`` if None). NULL forbidden post-M9.
        invalid_at: When the relation ceased to be true. NULL = still open.
        created_at: When the edge was first written to the graph (system time).
        expired_at: When the edge was logically deleted (Q3 soft-delete).
            NULL = live (not soft-deleted).
        superseded_by: Q1 supersede pointer — id of the replacement edge.
            NULL = this edge is current (or was hard-deleted, never superseded).
            The replacement edge's ``valid_at`` = this edge's ``invalid_at``.
    """

    source: str
    target: str
    relation: str
    weight: float
    confidence: EdgeConfidence
    confidence_score: float | None
    source_ids: list[str] = field(default_factory=list)
    # M9 bi-temporal fields (default None = open/live, M1-M8 compat).
    valid_at: datetime | None = None
    invalid_at: datetime | None = None
    created_at: datetime | None = None
    expired_at: datetime | None = None
    superseded_by: str | None = None


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
    "DEPRECATED": -1,
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


# ============================================================
# M9 exception subtree (architecture §16)
# ============================================================


class BiTemporalError(AudioGraphyError):
    """Base for M9 bi-temporal edge service errors."""


class BiTemporalInvalidRangeError(BiTemporalError):
    """Raised when valid_at >= invalid_at (open interval inverted)."""


class BiTemporalSupersedeChainError(BiTemporalError):
    """Raised when a supersede chain exceeds max depth or forms a cycle."""


class LeidenError(AudioGraphyError):
    """Base for M9 Leiden community-detection errors."""


class LeidenLibUnavailableError(LeidenError):
    """Raised when the preferred Leiden library cannot be imported.

    Per L2 the caller MUST fall back to full recompute + LRU cache rather
    than re-raising; this exception is raised only when the fallback also
    fails or when ``leiden_lib = "fail-fast"`` is configured.
    """


class LeidenThresholdExceededError(LeidenError):
    """Raised when the incremental diff exceeds the 30% threshold (L2).

    Caller should expand scope to full recompute rather than re-raise.
    """


class LeidenSnapshotCorruptError(LeidenError):
    """Raised when a PartitionSnapshot on disk cannot be deserialised."""


class CompressionError(AudioGraphyError):
    """Base for M9 compression service errors."""


class CompressionPolicyViolationError(CompressionError):
    """Raised when Q3 SOFT-only policy is violated (e.g. attempted hard delete)."""


class CompressionRollbackError(CompressionError):
    """Raised when a soft-delete batch cannot be rolled back."""


class SpeakerLinkerFuzzyError(AudioGraphyError):
    """Base for M9 speaker linker fuzzy-match errors."""


class SpeakerLinkerFuzzyThresholdError(SpeakerLinkerFuzzyError):
    """Raised when the configured threshold is outside [0, 1]."""


class SpeakerLinkerReconfirmUnavailableError(SpeakerLinkerFuzzyError):
    """Raised when L8 reconfirm is required but no voiceprint is available."""
