"""Graph builder — cross-chunk/cross-recording entity-relation merge into NetworkX.

Pipeline:
    1. Collect all entities from ExtractionResult[]
    2. Group by normalised name (entity_id)
    3. Merge: type majority vote, description dedup+concat, source_ids extend
    4. Detect AMBIGUOUS: same name, different types
    5. Collect all relations, group by (source, target, relation)
    6. Merge: weight accumulate, confidence upgrade (EXTRACTED > INFERRED > AMBIGUOUS)
    7. Compute node degree
    8. Write nodes + edges to NetworkXGraphStore
    9. Embed entities → MySQLVectorStore (if available)
    10. Save GraphML

Provenance: entity.source_ids = ["{recording_id}_{chunk_id}", ...] — the first
link in the 3-level provenance chain (entity → chunk → segment → recording).
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Sequence
from typing import TYPE_CHECKING

from audio_graphy.adapters.bundle import AdapterBundle
from audio_graphy.adapters.protocols import EdgeConfidence
from audio_graphy.core.extractor import ExtractionResult
from audio_graphy.core.types import (
    GraphEdge,
    GraphNode,
    GraphSnapshot,
    normalize_confidence_score,
    upgrade_confidence,
)

if TYPE_CHECKING:
    from audio_graphy.storage.graph_networkx import NetworkXGraphStore
    from audio_graphy.storage.mysql_vector import MySQLVectorStore

logger = logging.getLogger(__name__)

MAX_DESCRIPTION_LENGTH = 512
"""Maximum description length before truncation (A2 decision: M2 truncates, Phase 2 uses LLM)."""


class GraphBuilder:
    """Cross-chunk / cross-recording entity-relation merge into a NetworkX graph.

    Args:
        graph_store: NetworkXGraphStore for graph persistence.
        bundle: AdapterBundle (uses embed for entity vectors).
        vector_store: Optional MySQLVectorStore for entity embeddings.
        strict_persistence: Propagate GraphML checkpoint failures to callers
            that use the graph as a required, acknowledged projection.
    """

    def __init__(
        self,
        graph_store: NetworkXGraphStore,
        *,
        bundle: AdapterBundle | None = None,
        vector_store: MySQLVectorStore | None = None,
        strict_persistence: bool = False,
    ) -> None:
        self._graph_store = graph_store
        self._bundle = bundle
        self._vector_store = vector_store
        self._strict_persistence = strict_persistence

    async def build_from_extractions(
        self,
        extractions: Sequence[ExtractionResult],
        *,
        tenant_id: str = "default",
    ) -> GraphSnapshot:
        """Build a merged graph from extraction results.

        Args:
            extractions: Extraction results from multiple chunks/recordings.
            tenant_id: Tenant scope for vector store.

        Returns:
            GraphSnapshot with merged nodes and edges.
        """
        if not extractions:
            logger.warning("build_from_extractions called with empty extractions")
            return GraphSnapshot(
                nodes=[],
                edges=[],
                total_entities=0,
                total_relations=0,
                cross_recording_entities=0,
            )

        # Step 1: Merge entities
        merged_nodes, ambiguous_ids = self._merge_entities(extractions)

        # Step 2: Merge edges
        merged_edges = self._merge_edges(extractions, ambiguous_ids)

        # Step 3: Compute node degrees
        degree_map: dict[str, int] = defaultdict(int)
        for edge in merged_edges:
            degree_map[edge.source] += 1
            degree_map[edge.target] += 1

        # Update nodes with degree
        final_nodes = [
            GraphNode(
                entity_id=n.entity_id,
                name=n.name,
                type=n.type,
                description=n.description,
                source_ids=n.source_ids,
                recording_ids=n.recording_ids,
                degree=degree_map.get(n.entity_id, 0),
            )
            for n in merged_nodes
        ]

        # Step 4: Write to graph store
        for node in final_nodes:
            await self._graph_store.upsert_node(node)

        for edge in merged_edges:
            await self._graph_store.upsert_edge(edge)

        # Step 5: Save GraphML
        try:
            await self._graph_store.save()
        except Exception as exc:
            if self._strict_persistence:
                raise
            logger.warning("GraphML save failed (data preserved in memory): %s", exc)

        # Step 6: Embed entities → vector store
        if self._bundle is not None and self._vector_store is not None:
            await self._embed_entities(final_nodes, tenant_id)

        # Step 7: Compute cross-recording entities
        cross_recording = sum(1 for n in final_nodes if len(set(n.recording_ids)) >= 2)

        return GraphSnapshot(
            nodes=final_nodes,
            edges=merged_edges,
            total_entities=len(final_nodes),
            total_relations=len(merged_edges),
            cross_recording_entities=cross_recording,
        )

    # ------------------------------------------------------------------
    # Entity merging
    # ------------------------------------------------------------------

    def _merge_entities(
        self, extractions: Sequence[ExtractionResult]
    ) -> tuple[list[GraphNode], set[str]]:
        """Merge entities by normalised name.

        - Type: majority vote (Counter.most_common(1))
        - Description: dedup + concatenate, truncate if > 512 chars
        - source_ids: union of all "{recording_id}_{chunk_id}"
        - recording_ids: union of all recording_ids
        - AMBIGUOUS: if same name has multiple distinct types

        Args:
            extractions: All extraction results.

        Returns:
            Tuple of (merged_nodes, ambiguous_entity_ids).
        """
        # Group entities by normalised name
        groups: dict[str, list[tuple[str, str, int, int]]] = defaultdict(list)
        # name → list of (type, description, chunk_id, recording_id)

        for ext in extractions:
            for entity in ext.entities:
                groups[entity.name].append(
                    (entity.type, entity.description, entity.chunk_id, entity.recording_id)
                )

        merged: list[GraphNode] = []
        ambiguous_ids: set[str] = set()

        for name, entries in groups.items():
            # Type majority vote
            type_counter = Counter(e[0] for e in entries)
            best_type = type_counter.most_common(1)[0][0]

            # Check for AMBIGUOUS (multiple distinct types)
            if len(type_counter) > 1:
                ambiguous_ids.add(name)

            # Description dedup + concatenate
            descriptions: list[str] = []
            seen: set[str] = set()
            for _, desc, _, _ in entries:
                clean_desc = desc.strip()
                if clean_desc and clean_desc not in seen:
                    seen.add(clean_desc)
                    descriptions.append(clean_desc)
            description = " | ".join(descriptions)
            if len(description) > MAX_DESCRIPTION_LENGTH:
                description = description[:MAX_DESCRIPTION_LENGTH]

            # Source IDs (union, preserve order)
            source_ids: list[str] = []
            seen_sources: set[str] = set()
            for _, _, chunk_id, rec_id in entries:
                sid = f"{rec_id}_{chunk_id}"
                if sid not in seen_sources:
                    seen_sources.add(sid)
                    source_ids.append(sid)

            # Recording IDs (union)
            recording_ids: list[int] = []
            seen_recs: set[int] = set()
            for _, _, _, rec_id in entries:
                if rec_id not in seen_recs:
                    seen_recs.add(rec_id)
                    recording_ids.append(rec_id)

            merged.append(
                GraphNode(
                    entity_id=name,
                    name=name,
                    type=best_type,
                    description=description,
                    source_ids=source_ids,
                    recording_ids=recording_ids,
                    degree=0,  # Computed later
                )
            )

        return merged, ambiguous_ids

    # ------------------------------------------------------------------
    # Edge merging
    # ------------------------------------------------------------------

    def _merge_edges(
        self,
        extractions: Sequence[ExtractionResult],
        ambiguous_ids: set[str],
    ) -> list[GraphEdge]:
        """Merge relations by (source, target, relation).

        - Weight: accumulate
        - Confidence: upgrade (EXTRACTED > INFERRED > AMBIGUOUS)
        - source_ids: union
        - If source or target is AMBIGUOUS → edge confidence = AMBIGUOUS

        Args:
            extractions: All extraction results.
            ambiguous_ids: Set of entity IDs that are ambiguous.

        Returns:
            List of merged GraphEdge objects.
        """
        # Group by (source, target, relation)
        groups: dict[tuple[str, str, str], list[tuple[float, EdgeConfidence, str]]] = defaultdict(
            list
        )
        # key → list of (weight, confidence, source_id)

        for ext in extractions:
            for rel in ext.relations:
                source_id = f"{rel.recording_id}_{rel.chunk_id}"
                key = (rel.source_name, rel.target_name, rel.relation)
                groups[key].append((rel.weight, rel.confidence, source_id))

        merged: list[GraphEdge] = []
        for (source, target, relation), entries in groups.items():
            total_weight = sum(e[0] for e in entries)
            all_source_ids: list[str] = []
            seen: set[str] = set()
            for _, _, sid in entries:
                if sid not in seen:
                    seen.add(sid)
                    all_source_ids.append(sid)

            # Confidence upgrade
            final_confidence: EdgeConfidence = "AMBIGUOUS"
            for _, conf, _ in entries:
                final_confidence = upgrade_confidence(final_confidence, conf)

            # If either endpoint is AMBIGUOUS, downgrade edge to AMBIGUOUS
            if source in ambiguous_ids or target in ambiguous_ids:
                final_confidence = "AMBIGUOUS"

            score = normalize_confidence_score(final_confidence, total_weight)

            merged.append(
                GraphEdge(
                    source=source,
                    target=target,
                    relation=relation,
                    weight=total_weight,
                    confidence=final_confidence,
                    confidence_score=score,
                    source_ids=all_source_ids,
                )
            )

        return merged

    # ------------------------------------------------------------------
    # Entity embedding
    # ------------------------------------------------------------------

    async def _embed_entities(self, nodes: list[GraphNode], tenant_id: str) -> None:
        """Embed entity names + descriptions and store in vector store.

        Args:
            nodes: Merged graph nodes.
            tenant_id: Tenant scope.
        """
        assert self._bundle is not None
        assert self._vector_store is not None

        if not nodes:
            return

        texts = [f"{n.name} {n.description}" for n in nodes]
        try:
            embeddings = await self._bundle.embed.embed_texts(texts)
            for node, emb in zip(nodes, embeddings, strict=True):
                await self._vector_store.upsert_entity_vector(tenant_id, node.entity_id, emb.vector)
        except Exception as exc:
            logger.warning("Entity embedding failed (non-blocking): %s", exc)
