"""Dual-channel retriever — naive text + graph retrieval with time filtering.

Four-stage pipeline (DESIGN.md §3.3 stages 1-2):
    1a. Query rewrite + keyword extraction (weak_llm, cached)
    1b. Query embedding (embed adapter)
    2.  Naive channel: vector_store.search_chunks() → cosine top-k
    3.  Graph channel: keyword → entity match → 1-hop neighbors → reverse-lookup chunks
    4.  Union dedup (by chunk_id, score=max) + time filter + sort by recorded_at

The graph channel uses ``relation_counts`` (graph-structure signal) rather
than pure vector similarity — this is one of VideoRAG's three key innovations.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.adapters.bundle import AdapterBundle
from audio_graphy.core.types import GraphNode
from audio_graphy.models.chunk import Chunk
from audio_graphy.models.recording import Recording

if TYPE_CHECKING:
    from audio_graphy.storage.file_index import FileIndex
    from audio_graphy.storage.graph_networkx import NetworkXGraphStore
    from audio_graphy.storage.mysql_vector import MySQLVectorStore

logger = logging.getLogger(__name__)


# ============================================================
# Data classes
# ============================================================


@dataclass(frozen=True, slots=True)
class CandidateSegment:
    """A retrieval candidate segment.

    Attributes:
        chunk_id: Chunk database ID.
        recording_id: Recording ID.
        segment_ids: Segment indices within the recording.
        text: Chunk text content.
        recorded_at: Recording timestamp (for time filtering + sorting).
        score: Similarity score (naive=cosine, graph=relation_counts normalised).
        source_channel: "naive" or "graph".
    """

    chunk_id: int
    recording_id: int
    segment_ids: list[int]
    text: str
    recorded_at: datetime | None
    score: float
    source_channel: str


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """Dual-channel retrieval result.

    Attributes:
        query: Original query string.
        candidates: Merged + filtered + sorted candidate segments.
        naive_hits: Number of candidates from the naive channel.
        graph_hits: Number of candidates from the graph channel.
        filtered_by_time: Number of candidates removed by time filter.
    """

    query: str
    candidates: list[CandidateSegment]
    naive_hits: int
    graph_hits: int
    filtered_by_time: int


# ============================================================
# Dual-channel retriever
# ============================================================


class DualChannelRetriever:
    """Dual-channel retrieval + time filtering.

    Args:
        bundle: AdapterBundle (uses weak_llm for query rewrite + embed).
        vector_store: MySQLVectorStore for naive channel.
        graph_store: NetworkXGraphStore for graph channel.
        session_factory: Optional async session factory for chunk detail lookup.
        file_index: Optional FileIndex for chunk detail lookup (fallback).
    """

    def __init__(
        self,
        bundle: AdapterBundle,
        vector_store: MySQLVectorStore,
        graph_store: NetworkXGraphStore,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        file_index: FileIndex | None = None,
    ) -> None:
        self._bundle = bundle
        self._vector_store = vector_store
        self._graph_store = graph_store
        self._session_factory = session_factory
        self._file_index = file_index

    async def retrieve(
        self,
        query: str,
        *,
        tenant_id: str = "default",
        top_k: int = 10,
        time_range: tuple[datetime, datetime] | None = None,
    ) -> RetrievalResult:
        """Execute dual-channel retrieval.

        Args:
            query: Natural language query.
            tenant_id: Tenant scope.
            top_k: Max candidates per channel.
            time_range: Optional (start, end) for recorded_at filtering.

        Returns:
            RetrievalResult with merged, filtered, and sorted candidates.
        """
        # Step 1: Query embedding
        query_vec = await self._embed_query(query)

        # Step 2: Extract keywords for graph channel
        keywords = await self._extract_keywords(query)

        # Step 3: Dual-channel retrieval (parallel)
        naive_candidates = await self._naive_channel(query_vec, tenant_id, top_k)
        graph_candidates = await self._graph_channel(keywords, tenant_id, top_k)

        # Step 4: Union + dedup
        merged = self._union_dedup(naive_candidates, graph_candidates)

        # Step 5: Time filter
        filtered, removed_count = self._filter_by_time(merged, time_range)

        # Step 6: Sort by recorded_at
        sorted_candidates = self._sort_by_time(filtered)

        return RetrievalResult(
            query=query,
            candidates=sorted_candidates,
            naive_hits=len(naive_candidates),
            graph_hits=len(graph_candidates),
            filtered_by_time=removed_count,
        )

    # ------------------------------------------------------------------
    # Naive channel (vector search)
    # ------------------------------------------------------------------

    async def _naive_channel(
        self,
        query_vec: tuple[float, ...],
        tenant_id: str,
        top_k: int,
    ) -> list[CandidateSegment]:
        """Naive text chunk retrieval via brute-force cosine search.

        Args:
            query_vec: Query embedding vector.
            tenant_id: Tenant scope.
            top_k: Max results.

        Returns:
            List of CandidateSegment with source_channel="naive".
        """
        try:
            hits = await self._vector_store.search_chunks(tenant_id, query_vec, top_k=top_k)
        except Exception as exc:
            logger.warning("Naive channel vector search failed: %s", exc)
            return []

        if not hits:
            return []

        # Look up chunk details
        chunk_ids = [int(h.id) for h in hits if isinstance(h.id, int)]
        chunk_details = await self._lookup_chunks(chunk_ids)

        candidates: list[CandidateSegment] = []
        for hit in hits:
            if not isinstance(hit.id, int):
                continue
            detail = chunk_details.get(hit.id)
            if detail is None:
                continue
            candidates.append(
                CandidateSegment(
                    chunk_id=hit.id,
                    recording_id=detail["recording_id"],
                    segment_ids=detail["segment_ids"],
                    text=detail["text"],
                    recorded_at=detail["recorded_at"],
                    score=hit.score,
                    source_channel="naive",
                )
            )
        return candidates

    # ------------------------------------------------------------------
    # Graph channel (entity → neighbors → chunks)
    # ------------------------------------------------------------------

    async def _graph_channel(
        self,
        keywords: list[str],
        tenant_id: str,
        top_k: int,
    ) -> list[CandidateSegment]:
        """Graph retrieval: keyword → entity match → neighbors → reverse-lookup chunks.

        Args:
            keywords: Query keywords for entity matching.
            tenant_id: Tenant scope.
            top_k: Max results.

        Returns:
            List of CandidateSegment with source_channel="graph".
        """
        if not keywords:
            return []

        try:
            all_nodes = await self._graph_store.get_all_nodes()
        except Exception as exc:
            logger.warning("Graph channel: failed to get nodes: %s", exc)
            return []

        if not all_nodes:
            return []

        # Match keywords to entity names
        matched_entities: list[tuple[str, dict[str, int]]] = []
        for graph_node in all_nodes:
            for kw in keywords:
                if kw and (kw in graph_node.name or graph_node.name in kw):
                    # Get relation counts for this entity
                    counts = await self._graph_store.get_relation_counts(graph_node.entity_id)
                    matched_entities.append((graph_node.entity_id, counts))
                    break

        if not matched_entities:
            return []

        # Sort by total relation count (graph-structure signal)
        matched_entities.sort(
            key=lambda x: sum(x[1].values()) if x[1] else 0,
            reverse=True,
        )

        # Collect chunk_ids from matched entities + their neighbors
        chunk_score_map: dict[int, float] = {}
        max_count = max(
            (sum(c.values()) for _, c in matched_entities if c),
            default=1,
        )

        for entity_id, counts in matched_entities[:top_k]:
            total_count = sum(counts.values()) if counts else 0
            normalised_score = total_count / max_count if max_count > 0 else 0.0

            # Get the entity node to access source_ids
            node: GraphNode | None = await self._graph_store.get_node(entity_id)
            if node is None:
                continue

            # Reverse-lookup: source_ids → chunk_ids
            for source_id in node.source_ids:
                chunk_id = self._parse_chunk_id(source_id)
                if chunk_id is not None and (
                    chunk_id not in chunk_score_map or normalised_score > chunk_score_map[chunk_id]
                ):
                    chunk_score_map[chunk_id] = normalised_score

            # Also get neighbors' source_ids
            neighbors = await self._graph_store.get_neighbors(entity_id, max_hops=1)
            for neighbor in neighbors:
                for source_id in neighbor.source_ids:
                    chunk_id = self._parse_chunk_id(source_id)
                    if chunk_id is not None:
                        neighbor_score = normalised_score * 0.5  # Neighbors get lower score
                        if (
                            chunk_id not in chunk_score_map
                            or neighbor_score > chunk_score_map[chunk_id]
                        ):
                            chunk_score_map[chunk_id] = neighbor_score

        if not chunk_score_map:
            return []

        # Look up chunk details
        chunk_details = await self._lookup_chunks(list(chunk_score_map.keys()))

        candidates: list[CandidateSegment] = []
        for chunk_id, score in chunk_score_map.items():
            detail = chunk_details.get(chunk_id)
            if detail is None:
                continue
            candidates.append(
                CandidateSegment(
                    chunk_id=chunk_id,
                    recording_id=detail["recording_id"],
                    segment_ids=detail["segment_ids"],
                    text=detail["text"],
                    recorded_at=detail["recorded_at"],
                    score=score,
                    source_channel="graph",
                )
            )
        return candidates

    # ------------------------------------------------------------------
    # Union + dedup
    # ------------------------------------------------------------------

    @staticmethod
    def _union_dedup(
        naive: list[CandidateSegment],
        graph: list[CandidateSegment],
    ) -> list[CandidateSegment]:
        """Merge naive + graph candidates, dedup by chunk_id, score=max.

        Args:
            naive: Naive channel candidates.
            graph: Graph channel candidates.

        Returns:
            Merged candidate list (no duplicate chunk_ids).
        """
        merged: dict[int, CandidateSegment] = {}
        for cand in naive + graph:
            existing = merged.get(cand.chunk_id)
            if existing is None or cand.score > existing.score:
                merged[cand.chunk_id] = cand
        return list(merged.values())

    # ------------------------------------------------------------------
    # Time filter
    # ------------------------------------------------------------------

    @staticmethod
    def _filter_by_time(
        candidates: list[CandidateSegment],
        time_range: tuple[datetime, datetime] | None,
    ) -> tuple[list[CandidateSegment], int]:
        """Filter candidates by recorded_at time range.

        Args:
            candidates: Input candidates.
            time_range: (start, end) or None for no filtering.

        Returns:
            Tuple of (filtered_candidates, removed_count).
        """
        if time_range is None:
            return candidates, 0

        start, end = time_range
        # Normalize start/end to offset-aware if needed
        from datetime import UTC as _UTC

        if start.tzinfo is None:
            start = start.replace(tzinfo=_UTC)
        if end.tzinfo is None:
            end = end.replace(tzinfo=_UTC)

        filtered: list[CandidateSegment] = []
        removed = 0
        for cand in candidates:
            if cand.recorded_at is None:
                # No recorded_at — keep (can't filter)
                filtered.append(cand)
            else:
                rec_at = cand.recorded_at
                if rec_at.tzinfo is None:
                    rec_at = rec_at.replace(tzinfo=_UTC)
                if start <= rec_at <= end:
                    filtered.append(cand)
                else:
                    removed += 1
        return filtered, removed

    # ------------------------------------------------------------------
    # Sort by recorded_at
    # ------------------------------------------------------------------

    @staticmethod
    def _sort_by_time(
        candidates: list[CandidateSegment],
    ) -> list[CandidateSegment]:
        """Sort candidates by recorded_at ascending (None last).

        Args:
            candidates: Input candidates.

        Returns:
            Sorted candidates.
        """
        return sorted(
            candidates,
            key=lambda c: (c.recorded_at is None, c.recorded_at or datetime.min),
        )

    # ------------------------------------------------------------------
    # Query embedding
    # ------------------------------------------------------------------

    async def _embed_query(self, query: str) -> tuple[float, ...]:
        """Embed the query using the embed adapter.

        Args:
            query: Query string.

        Returns:
            Query embedding vector.

        Raises:
            Exception: If embedding fails (propagates — no vector = no retrieval).
        """
        results = await self._bundle.embed.embed_texts([query])
        return results[0].vector

    # ------------------------------------------------------------------
    # Keyword extraction
    # ------------------------------------------------------------------

    async def _extract_keywords(self, query: str) -> list[str]:
        """Extract keywords from query for graph channel entity matching.

        Tries weak_llm first, falls back to simple Chinese text segmentation.

        Args:
            query: Natural language query.

        Returns:
            List of keyword strings.
        """
        # Try LLM-based keyword extraction
        try:
            messages: list[dict[str, str]] = [
                {
                    "role": "user",
                    "content": f"请从以下问题中提取关键词，用逗号分隔返回：\n{query}",
                }
            ]
            cache_key = self._compute_cache_key(self._bundle.weak_llm.model, messages)

            # Check file_index cache (Layer 2)
            if self._file_index is not None:
                cached = await self._file_index.get_llm_cache(cache_key)
                if cached is not None:
                    return self._parse_keywords(cached)

            response = await self._bundle.weak_llm.complete(messages=messages, cache_key=cache_key)

            # Store in file_index
            if self._file_index is not None and not response.cached:
                await self._file_index.set_llm_cache(cache_key, response.text)

            keywords = self._parse_keywords(response.text)
            if keywords:
                return keywords
        except Exception as exc:
            logger.warning("LLM keyword extraction failed, using fallback: %s", exc)

        # Fallback: simple Chinese segmentation
        return self._fallback_keywords(query)

    @staticmethod
    def _parse_keywords(text: str) -> list[str]:
        """Parse comma/separator-delimited keywords from LLM response.

        Args:
            text: LLM response text.

        Returns:
            List of keyword strings.
        """
        # Remove common prefixes
        text = re.sub(r"^(关键词|keywords?)[:：]?\s*", "", text, flags=re.IGNORECASE)
        # Split by common delimiters
        parts = re.split(r"[,，;；\n、]+", text)
        return [p.strip() for p in parts if p.strip() and len(p.strip()) >= 2]

    @staticmethod
    def _fallback_keywords(query: str) -> list[str]:
        """Simple keyword extraction: split by punctuation and spaces.

        Args:
            query: Query string.

        Returns:
            List of keyword strings.
        """
        parts = re.split(r"[，。？！,.!?;\s\n、（）()]+", query)
        return [p.strip() for p in parts if p.strip() and len(p.strip()) >= 2]

    # ------------------------------------------------------------------
    # Chunk detail lookup
    # ------------------------------------------------------------------

    async def _lookup_chunks(self, chunk_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Look up chunk details (text, segment_ids, recording_id, recorded_at).

        Uses MySQL if session_factory is available, otherwise file_index.

        Args:
            chunk_ids: Chunk database IDs.

        Returns:
            Dict mapping chunk_id → {recording_id, segment_ids, text, recorded_at}.
        """
        if not chunk_ids:
            return {}

        if self._session_factory is not None:
            return await self._lookup_chunks_mysql(chunk_ids)
        if self._file_index is not None:
            return await self._lookup_chunks_file_index(chunk_ids)
        return {}

    async def _lookup_chunks_mysql(self, chunk_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Look up chunk details from MySQL.

        Args:
            chunk_ids: Chunk database IDs.

        Returns:
            Dict mapping chunk_id → details.
        """
        result: dict[int, dict[str, Any]] = {}
        assert self._session_factory is not None
        async with self._session_factory() as session:
            stmt = (
                select(Chunk, Recording.recorded_at)
                .outerjoin(Recording, Chunk.recording_id == Recording.id)
                .where(Chunk.id.in_(chunk_ids))
            )
            rows = await session.execute(stmt)
            for chunk, recorded_at in rows:
                result[chunk.id] = {
                    "recording_id": chunk.recording_id,
                    "segment_ids": list(chunk.segment_ids) if chunk.segment_ids else [],
                    "text": chunk.text,
                    "recorded_at": recorded_at,
                }
        return result

    async def _lookup_chunks_file_index(self, chunk_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Look up chunk details from file_index (fallback).

        Args:
            chunk_ids: Chunk database IDs.

        Returns:
            Dict mapping chunk_id → details.
        """
        assert self._file_index is not None
        result: dict[int, dict[str, Any]] = {}
        all_chunks = await self._file_index.get_all("kv_store_text_chunks")
        await self._file_index.get_all("kv_store_video_segments")
        all_paths = await self._file_index.get_all("kv_store_video_path")

        for chunk_id in chunk_ids:
            # Search all chunk entries for matching chunk_id
            for key, data in all_chunks.items():
                if str(chunk_id) in key or key.endswith(f"_{chunk_id}"):
                    recording_id = data.get("recording_id", 0)
                    recorded_at_str = all_paths.get(str(recording_id), {}).get("recorded_at")
                    recorded_at = None
                    if recorded_at_str:
                        with contextlib.suppress(ValueError, TypeError):
                            recorded_at = datetime.fromisoformat(recorded_at_str)
                    result[chunk_id] = {
                        "recording_id": recording_id,
                        "segment_ids": data.get("segment_ids", []),
                        "text": data.get("text", ""),
                        "recorded_at": recorded_at,
                    }
                    break
        return result

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_chunk_id(source_id: str) -> int | None:
        """Parse chunk_id from source_id format "{recording_id}_{chunk_id}".

        Args:
            source_id: Source ID string.

        Returns:
            Chunk ID as int, or None if unparseable.
        """
        parts = source_id.rsplit("_", 1)
        if len(parts) == 2:
            try:
                return int(parts[1])
            except ValueError:
                return None
        return None

    @staticmethod
    def _compute_cache_key(model: str, messages: Sequence[dict[str, str]]) -> str:
        """Compute LLM cache key = MD5(model, messages)."""
        payload = json.dumps(
            {"model": model, "messages": list(messages)},
            ensure_ascii=False,
        )
        return hashlib.md5(payload.encode("utf-8")).hexdigest()
