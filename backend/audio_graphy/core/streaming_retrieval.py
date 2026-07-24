"""StreamingRetriever — lightweight retrieval over the streaming-updated subgraph.

M8 Phase 4 (WS-3 / T10). Per architecture §11: unlike the full M7
``ThreeChannelRetriever``, this retriever runs the **graph channel only**
over the subgraph that ``DeltaGraphUpdater`` keeps incrementally updated
during a live session. The naive / audio channels depend on pre-built
vector stores and are intentionally out of scope for the streaming path
(§11.3: M7 three-channel weights 0.5 / 0.3 / 0.2 are unchanged — they
simply do not apply here).

Q3 decision (§11.2) — edge confidence handling:

    ==============  =================  =================================
    confidence_tag  default multiplier  strict mode (min_confidence)
    ==============  =================  =================================
    EXTRACTED       1.0                included
    INFERRED        0.8                filtered when min >= EXTRACTED
    AMBIGUOUS       0.5                filtered when min >= INFERRED
    DEPRECATED      (dropped)          always filtered (M9 L7)
    ==============  =================  =================================

RWLock discipline (shared knowledge §17):
    All graph reads happen under ``rwlock.read_lock()`` so concurrent
    ``DeltaGraphUpdater`` writes (write-lock, exclusive) never race with
    retrieval. Reads never block other reads.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from audio_graphy.adapters.protocols import EdgeConfidence

if TYPE_CHECKING:
    from audio_graphy.adapters.bundle import AdapterBundle
    from audio_graphy.core.streaming_rwlock import StreamingRWLock
    from audio_graphy.storage.graph_networkx import NetworkXGraphStore

logger = logging.getLogger(__name__)

# Q3 locked multipliers (§11.2). Config defaults match Settings fields
# ``streaming_ambiguous_edge_weight`` / ``streaming_inferred_edge_weight``.
DEFAULT_AMBIGUOUS_EDGE_WEIGHT = 0.5
DEFAULT_INFERRED_EDGE_WEIGHT = 0.8
EXTRACTED_EDGE_WEIGHT = 1.0

# Confidence rank used for min_confidence strict-mode filtering.
# DEPRECATED (M9 L7) is ranked below AMBIGUOUS so it is always filtered
# in strict mode; the Q3 multiplier path also short-circuits DEPRECATED
# to weight 0.0 so it never contributes to retrieval scoring.
_CONFIDENCE_RANK: dict[str, int] = {
    "DEPRECATED": -1,
    "AMBIGUOUS": 0,
    "INFERRED": 1,
    "EXTRACTED": 2,
}

GraphStoreFactory = Callable[[str], "NetworkXGraphStore"]


@dataclass(frozen=True, slots=True)
class StreamingCandidate:
    """One streaming retrieval candidate.

    Attributes:
        entity_id: Matched entity node id (canonical name).
        entity_name: Display name.
        entity_type: Domain type.
        relation: Relation of the best edge ("" for direct entity match).
        weight: Confidence-adjusted edge weight (or 1.0 for direct match).
        confidence: Confidence tag of the best edge ("EXTRACTED" for direct).
        source_ids: Provenance — ``"{recording_id}_{chunk_id}"`` strings.
        depth: 0 = keyword-matched entity, 1 = 1-hop neighbor.
    """

    entity_id: str
    entity_name: str
    entity_type: str
    relation: str
    weight: float
    confidence: EdgeConfidence
    source_ids: list[str] = field(default_factory=list)
    depth: int = 0

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form for the ``retrieval_result`` WS event."""
        return {
            "entity_id": self.entity_id,
            "entity_name": self.entity_name,
            "entity_type": self.entity_type,
            "relation": self.relation,
            "weight": round(self.weight, 4),
            "confidence": self.confidence,
            "source_ids": list(self.source_ids),
            "depth": self.depth,
        }


@dataclass(frozen=True, slots=True)
class StreamingRetrievalResult:
    """Result of one ``StreamingRetriever.retrieve()`` call.

    Attributes:
        query: Original query string.
        tenant_id: Tenant scope.
        keywords: Keywords extracted from the query.
        candidates: Confidence-weighted candidates, sorted by weight desc.
        filtered_by_confidence: Candidates removed by min_confidence.
    """

    query: str
    tenant_id: str
    keywords: list[str]
    candidates: list[StreamingCandidate]
    filtered_by_confidence: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "tenant_id": self.tenant_id,
            "keywords": list(self.keywords),
            "candidates": [c.to_dict() for c in self.candidates],
            "filtered_by_confidence": self.filtered_by_confidence,
        }


class StreamingRetriever:
    """Graph-channel retrieval over the live streaming subgraph.

    Args:
        graph_store_factory: Per-tenant ``NetworkXGraphStore`` factory —
            MUST return the same store instance ``DeltaGraphUpdater``
            writes into (per-tenant registry).
        rwlock: The same ``StreamingRWLock`` guarding the graph.
        bundle: AdapterBundle (uses ``weak_llm`` for keyword extraction;
            falls back to regex segmentation on failure).
        ambiguous_edge_weight: Q3 multiplier for AMBIGUOUS edges (default 0.5).
        inferred_edge_weight: Q3 multiplier for INFERRED edges (default 0.8).
    """

    def __init__(
        self,
        graph_store_factory: GraphStoreFactory,
        rwlock: StreamingRWLock,
        bundle: AdapterBundle,
        *,
        ambiguous_edge_weight: float = DEFAULT_AMBIGUOUS_EDGE_WEIGHT,
        inferred_edge_weight: float = DEFAULT_INFERRED_EDGE_WEIGHT,
    ) -> None:
        if not 0.0 <= ambiguous_edge_weight <= 1.0:
            raise ValueError(f"ambiguous_edge_weight must be in [0,1], got {ambiguous_edge_weight}")
        if not 0.0 <= inferred_edge_weight <= 1.0:
            raise ValueError(f"inferred_edge_weight must be in [0,1], got {inferred_edge_weight}")
        self._graph_store_factory = graph_store_factory
        self._rwlock = rwlock
        self._bundle = bundle
        self._ambiguous_w = ambiguous_edge_weight
        self._inferred_w = inferred_edge_weight

    async def retrieve(
        self,
        query: str,
        *,
        tenant_id: str = "default",
        session_id: str | None = None,
        top_k: int = 5,
        min_confidence: EdgeConfidence | None = None,
    ) -> StreamingRetrievalResult:
        """Graph-channel-only retrieval over the live subgraph.

        Steps (§11.1):
            1. weak_llm extract keywords (fallback: regex segmentation).
            2. ``get_all_nodes()`` under ``rwlock.read_lock()``.
            3. Keyword → entity match; 1-hop neighbors with edge weights
               adjusted by confidence tag (EXTRACTED ×1.0 / INFERRED ×0.8 /
               AMBIGUOUS ×0.5).
            4. ``min_confidence`` strict mode filters low-confidence edges.
            5. Sort by adjusted weight desc, cap at ``top_k``.

        Args:
            query: Natural-language query.
            tenant_id: Tenant scope.
            session_id: Optional session id (telemetry / future filtering).
            top_k: Max candidates returned.
            min_confidence: Strict mode — drop edges below this confidence.

        Returns:
            StreamingRetrievalResult.
        """
        keywords = await self._extract_keywords(query)
        if not keywords:
            return StreamingRetrievalResult(
                query=query,
                tenant_id=tenant_id,
                keywords=[],
                candidates=[],
            )

        graph_store = self._graph_store_factory(tenant_id)

        async with self._rwlock.read_lock():
            all_nodes = await graph_store.get_all_nodes()
            candidates: list[StreamingCandidate] = []
            filtered = 0
            for node in all_nodes:
                if not self._matches_any(node.name, keywords):
                    continue
                # Depth-0 direct match (weight 1.0, EXTRACTED-equivalent).
                candidates.append(
                    StreamingCandidate(
                        entity_id=node.entity_id,
                        entity_name=node.name,
                        entity_type=node.type,
                        relation="",
                        weight=EXTRACTED_EDGE_WEIGHT,
                        confidence="EXTRACTED",
                        source_ids=list(node.source_ids),
                        depth=0,
                    )
                )
                # Depth-1 neighbors via edges with Q3 confidence weighting.
                edges = await graph_store.get_edges(node.entity_id)
                for edge in edges:
                    # M9 L7 — DEPRECATED edges are excluded entirely
                    # (multiplier × 0; do not appear in any candidate set).
                    if edge.confidence == "DEPRECATED":
                        filtered += 1
                        continue
                    if not self._passes_min_confidence(edge.confidence, min_confidence):
                        filtered += 1
                        continue
                    neighbor_id = edge.target if edge.source == node.entity_id else edge.source
                    neighbor = await graph_store.get_node(neighbor_id)
                    if neighbor is None:
                        continue
                    adjusted = edge.weight * self._confidence_multiplier(edge.confidence)
                    candidates.append(
                        StreamingCandidate(
                            entity_id=neighbor.entity_id,
                            entity_name=neighbor.name,
                            entity_type=neighbor.type,
                            relation=edge.relation,
                            weight=adjusted,
                            confidence=edge.confidence,
                            source_ids=list(edge.source_ids),
                            depth=1,
                        )
                    )

        # Dedup by (entity_id, depth) keeping max weight.
        dedup: dict[tuple[str, int], StreamingCandidate] = {}
        for cand in candidates:
            key = (cand.entity_id, cand.depth)
            existing = dedup.get(key)
            if existing is None or cand.weight > existing.weight:
                dedup[key] = cand

        ranked = sorted(dedup.values(), key=lambda c: c.weight, reverse=True)[: max(1, top_k)]
        return StreamingRetrievalResult(
            query=query,
            tenant_id=tenant_id,
            keywords=keywords,
            candidates=ranked,
            filtered_by_confidence=filtered,
        )

    # ------------------------------------------------------------------
    # Confidence helpers (Q3)
    # ------------------------------------------------------------------
    def _confidence_multiplier(self, confidence: EdgeConfidence) -> float:
        """Map confidence tag → Q3 weight multiplier."""
        if confidence == "EXTRACTED":
            return EXTRACTED_EDGE_WEIGHT
        if confidence == "INFERRED":
            return self._inferred_w
        return self._ambiguous_w

    @staticmethod
    def _passes_min_confidence(
        confidence: EdgeConfidence,
        min_confidence: EdgeConfidence | None,
    ) -> bool:
        """Strict-mode filter: confidence rank must be >= min rank."""
        if min_confidence is None:
            return True
        return _CONFIDENCE_RANK[confidence] >= _CONFIDENCE_RANK[min_confidence]

    # ------------------------------------------------------------------
    # Keyword extraction (reuses M7 fallback strategy)
    # ------------------------------------------------------------------
    async def _extract_keywords(self, query: str) -> list[str]:
        """Extract keywords via weak_llm; fall back to regex segmentation."""
        try:
            messages: list[dict[str, str]] = [
                {
                    "role": "user",
                    "content": f"请从以下问题中提取关键词，用逗号分隔返回：\n{query}",
                }
            ]
            response = await self._bundle.weak_llm.complete(messages=messages)
            keywords = self._parse_keywords(response.text)
            if keywords:
                return keywords
        except Exception as exc:
            logger.warning("StreamingRetriever LLM keyword extraction failed: %s", exc)
        return self._fallback_keywords(query)

    @staticmethod
    def _parse_keywords(text: str) -> list[str]:
        text = re.sub(r"^(关键词|keywords?)[:：]?\s*", "", text, flags=re.IGNORECASE)
        parts = re.split(r"[,，;；\n、]+", text)
        return [p.strip() for p in parts if p.strip() and len(p.strip()) >= 2]

    @staticmethod
    def _fallback_keywords(query: str) -> list[str]:
        parts = re.split(r"[，。？！,.!?;\s\n、（）()]+", query)
        return [p.strip() for p in parts if p.strip() and len(p.strip()) >= 2]

    @staticmethod
    def _matches_any(name: str, keywords: list[str]) -> bool:
        """Substring match in either direction (mirrors M7 graph channel)."""
        return any(kw and (kw in name or name in kw) for kw in keywords)


__all__ = [
    "DEFAULT_AMBIGUOUS_EDGE_WEIGHT",
    "DEFAULT_INFERRED_EDGE_WEIGHT",
    "EXTRACTED_EDGE_WEIGHT",
    "StreamingCandidate",
    "StreamingRetrievalResult",
    "StreamingRetriever",
]
